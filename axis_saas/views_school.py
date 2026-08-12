import re
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, Http404
from django.contrib import messages
from django.db.models import Sum, Q, Exists, OuterRef, Max
from django.db.models.functions import TruncMonth, TruncDay
from django.db.models import Count
from django.core.paginator import Paginator
from django.db import connection
from django_tenants.utils import schema_context
from decimal import Decimal
from datetime import date, timedelta, datetime
from collections import defaultdict
import json
import re
from functools import wraps
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.views.decorators.http import require_http_methods




def require_tenant_type(allowed_types):
    def decorator(view_func):
        def wrapper(request, schema_name, *args, **kwargs):
            # Use request.tenant if already set (by portal_wrapper)
            if hasattr(request, 'tenant') and request.tenant is not None:
                tenant = request.tenant
            else:
                tenant = get_tenant(request, schema_name)
            if tenant.tenant_type not in allowed_types:
                raise Http404("Not available for this tenant type")
            return view_func(request, schema_name, *args, **kwargs)
        return wrapper
    return decorator




def require_school_feature(feature_key):
    def decorator(view_func):
        def wrapper(request, schema_name, *args, **kwargs):
            if hasattr(request, 'tenant') and request.tenant is not None:
                tenant = request.tenant
            else:
                tenant = get_tenant(request, schema_name)
            if tenant.tenant_type != 'school' or not tenant.is_feature_enabled(feature_key):
                raise Http404("This school feature is not enabled for this tenant.")
            return view_func(request, schema_name, *args, **kwargs)
        return wrapper
    return decorator


from .models import SchoolClient, Student, FeeStructure, FeeRecord, PaymentTransaction, SchoolFeeSettings, Product, ProductCategory

# --- Helper to create notification for fee generation ---


def create_fee_generation_notification(schema_name, month, year, created_count, triggered_by, mobile=False):
    """Create a notification for fee generation."""
    from .models import Notification
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        message = f"Fee vouchers generated for {month}/{year}: {created_count} records created."
        if mobile:
            link = f"/portal/{schema_name}/vouchers/mobile/?month={month}&year={year}"
        else:
            link = f"/portal/{schema_name}/vouchers/?month={month}&year={year}"
        Notification.objects.create(message=message, link=link)

from .forms import StudentForm, FeeCollectionForm, FeeSettingsForm, FeeStructureForm, FamilyPaymentForm
from django.http import JsonResponse, HttpResponse
from django.db import transaction
from .models import ManualGenerationLog






def get_overall_pending(student):
    """Compute overall remaining balance: total fee + total items cost - total paid."""
    from decimal import Decimal
    from django.db.models import Sum
    total_fee = Decimal('0')
    for fr in student.fee_records.all():
        total_fee += fr.total_amount
    total_paid = student.payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    # Compute total items cost from all payments
    total_items_cost = Decimal('0')
    for p in student.payments.all():
        items = _extract_item_sales_from_remarks(p.remarks or '')
        total_items_cost += sum(item['line_total'] for item in items)
    return total_fee + total_items_cost - total_paid




def local_time_str(dt):
    """Convert aware datetime to local timezone and return formatted time string."""
    if not dt:
        return ''
    from django.utils import timezone
    local = timezone.localtime(dt)
    return local.strftime('%H:%M')




def get_tenant(request, schema_name):
    from django_tenants.utils import schema_context
    with schema_context('public'):
        return get_object_or_404(SchoolClient, schema_name=schema_name)


def create_student_from_payload(schema_name, payload):
    with schema_context(schema_name):
        form = StudentForm(payload)
        if not form.is_valid():
            return None, form

        student = form.save(commit=False)
        if not student.custom_fee:
            fee_struct = FeeStructure.objects.filter(grade=student.grade).first()
            if fee_struct:
                student.custom_fee = fee_struct.monthly_fee
        student.save()
        return student, form


def update_student_from_payload(schema_name, student_id, payload):
    with schema_context(schema_name):
        student = get_object_or_404(Student, id=student_id)
        form = StudentForm(payload, instance=student)
        if not form.is_valid():
            return None, form

        student = form.save(commit=False)
        if not student.custom_fee:
            fee_struct = FeeStructure.objects.filter(grade=student.grade).first()
            if fee_struct:
                student.custom_fee = fee_struct.monthly_fee
        student.save()
        return student, form


def build_payment_remarks(remarks, fee_to_apply, item_breakdown, temp_receipt=None):
    combined_remarks = (remarks or '').strip()
    item_details = '; '.join([
        f"{product.name} x{qty} @ ₹{product.selling_price} = ₹{line_total:.2f}"
        for product, qty, line_total in item_breakdown
    ]) if item_breakdown else ''

    if fee_to_apply > 0 and item_breakdown:
        combined_remarks = (combined_remarks + '\n' if combined_remarks else '') + (
            f"Fee payment applied: ₹{fee_to_apply:.2f}. Items sold: {item_details}"
        )
    elif fee_to_apply > 0:
        combined_remarks = combined_remarks or 'Fee payment'
    elif item_breakdown:
        combined_remarks = (combined_remarks + '\n' if combined_remarks else '') + ('Items sold: ' + item_details)

    if temp_receipt:
        combined_remarks = (combined_remarks + '\n' if combined_remarks else '') + f"Offline temp receipt: {temp_receipt}"

    return combined_remarks or 'Payment recorded offline.'


def apply_fee_payment(student, amount, payment_mode, remarks, product_items, created_by='admin'):
    if amount is None or amount <= 0:
        raise ValueError('Invalid payment amount')

    product_total = Decimal('0.00')
    item_breakdown = []
    for item in product_items or []:
        try:
            product_id = int(item.get('product_id'))
            qty = int(item.get('quantity', 0))
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue

        product = Product.objects.filter(id=product_id).first()
        if not product:
            raise ValueError(f'Product {product_id} not found')
        if product.quantity < qty:
            raise ValueError(f'Only {product.quantity} units available for {product.name}')

        line_total = product.selling_price * qty
        product_total += line_total
        item_breakdown.append((product, qty, line_total))

    pending_records = student.fee_records.filter(status__in=['pending', 'partial', 'overdue']).order_by('due_date')
    fee_pending = sum(r.remaining_total for r in pending_records)
    total_due = fee_pending + product_total

    fee_to_apply = min(amount, fee_pending) if fee_pending else Decimal('0.00')
    item_to_apply = max(amount - fee_to_apply, Decimal('0.00'))
    item_to_apply = min(item_to_apply, product_total) if product_total else Decimal('0.00')

    remaining = fee_to_apply
    paid_records = []
    for record in pending_records:
        if remaining <= 0:
            break
        due = record.remaining_total
        apply_now = min(due, remaining)
        record.paid_amount += apply_now
        remaining -= apply_now
        record.save()
        paid_records.append(record)

    combined_remarks = build_payment_remarks(remarks, fee_to_apply, item_breakdown)

    payment_record = PaymentTransaction.objects.create(
        student=student,
        amount=amount,
        payment_mode=payment_mode,
        payment_type='full' if amount >= total_due else 'partial',
        remarks=combined_remarks,
        created_by=created_by
    )

    if paid_records:
        payment_record.fee_records.set(paid_records)

    if item_breakdown:
        for product, qty, _ in item_breakdown:
            product.quantity -= qty
            product.save(update_fields=['quantity'])

    return payment_record, total_due


MOBILE_AGENT_RE = re.compile(r"Mobile|Android|iP(hone|od|ad)|Opera Mini|IEMobile|BlackBerry|webOS|Fennec|Silk", re.I)



def is_mobile_user_agent(request):
    ua = request.META.get('HTTP_USER_AGENT', '')
    return bool(MOBILE_AGENT_RE.search(ua))



def get_dashboard_context(tenant, schema_name):
    with schema_context(schema_name):
        today = timezone.localdate()
        first_day_month = today.replace(day=1)

        # ---- Core financials ----
        today_collection = PaymentTransaction.objects.filter(payment_date=today).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        month_collection = PaymentTransaction.objects.filter(payment_date__gte=first_day_month).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        total_revenue = PaymentTransaction.objects.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')

        total_pending = Decimal('0')
        for student in Student.objects.all():
            total_pending += get_overall_pending(student)

        defaulters_count = Student.objects.filter(fee_records__status__in=['pending', 'partial', 'overdue']).distinct().count()
        total_students = Student.objects.count()
        low_stock_count = Product.objects.filter(quantity__lt=10).count()

        total_billed = total_revenue + total_pending
        collection_rate = (float(total_revenue) / float(total_billed) * 100) if total_billed > 0 else 0

        recent_payments = list(PaymentTransaction.objects.select_related('student').order_by('-payment_date')[:5])

        top_defaulters = []
        for student in Student.objects.all():
            pending = get_overall_pending(student)
            if pending > 0:
                fee_pending = sum(fr.remaining_total for fr in student.fee_records.filter(status__in=['pending', 'partial', 'overdue']))
                top_defaulters.append({'student': student, 'pending': pending, 'fee_pending': fee_pending})
        top_defaulters = sorted(top_defaulters, key=lambda x: x['pending'], reverse=True)[:5]

        months_labels = []
        months_amounts = []
        for i in range(5, -1, -1):
            m = today.month - i
            y = today.year
            if m <= 0:
                m += 12
                y -= 1
            total = PaymentTransaction.objects.filter(payment_date__year=y, payment_date__month=m).aggregate(Sum('amount'))['amount__sum'] or 0
            months_labels.append(f"{m}/{y}")
            months_amounts.append(float(total))

    return {
        'tenant': tenant,
        'today_collection': today_collection,
        'month_collection': month_collection,
        'total_revenue': total_revenue,
        'total_pending': total_pending,
        'defaulters_count': defaulters_count,
        'total_students': total_students,
        'low_stock_count': low_stock_count,
        'collection_rate': round(collection_rate, 1),
        'recent_payments': recent_payments,
        'top_defaulters': top_defaulters,
        'months_labels': months_labels,
        'months_amounts': months_amounts,
        'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        'today': today,
        'start_date': first_day_month,
    }

# ------------------- Dashboard -------------------


@require_tenant_type(['school'])
@require_school_feature('dashboard')
def dashboard(request, schema_name):
    tenant = get_tenant(request, schema_name)
    context = get_dashboard_context(tenant, schema_name)
    return render(request, 'tenant/dashboard.html', context)



@require_tenant_type(['school'])
@require_school_feature('dashboard')
def mobile_dashboard(request, schema_name):
    tenant = get_tenant(request, schema_name)
    context = get_dashboard_context(tenant, schema_name)

    # ---- Notification banner logic ----
    with schema_context(schema_name):
        from .models import FeeRecord
        current_max_id = FeeRecord.objects.aggregate(Max('id'))['id__max'] or 0
        last_seen_id = request.session.get('last_fee_record_id')
        if last_seen_id is None:
            # First visit: set to current max, no banner
            request.session['last_fee_record_id'] = current_max_id
            show_banner = False
            new_count = 0
        elif current_max_id > last_seen_id:
            show_banner = True
            new_count = current_max_id - last_seen_id
            # Don't update session yet; will update on dismiss or view vouchers
        else:
            show_banner = False
            new_count = 0

        # If the request has a dismiss parameter, update session and reload
        if request.GET.get('dismiss_notification') == '1':
            request.session['last_fee_record_id'] = current_max_id
            show_banner = False
            new_count = 0

    context['show_notification_banner'] = show_banner
    
    context['new_vouchers_count'] = new_count
    # ---- Get latest fee month/year ----
    with schema_context(schema_name):
        from .models import FeeRecord
        latest_fee = FeeRecord.objects.order_by('-id').first()
        if latest_fee:
            context['voucher_month'] = latest_fee.month
            context['voucher_year'] = latest_fee.year
        else:
            context['voucher_month'] = None
            context['voucher_year'] = None
    # ---- End month/year logic ----

    # ---- End banner logic ----

    return render(request, 'mobile/dashboard.html', context)


@require_tenant_type(['school'])
@require_school_feature('dashboard')
def mobile_more(request, schema_name):
    tenant = get_tenant(request, schema_name)
    return render(request, 'mobile/more.html', {'tenant': tenant})



@require_tenant_type(['school'])
@require_school_feature('fee_collection')
def mobile_fee_collection(request, schema_name, student_id=None):
    return fee_collection(request, schema_name, student_id, force_mobile=True)



def get_student_list_context(request, schema_name):
    tenant = get_tenant(request, schema_name)
    query = request.GET.get('q', '')
    grade = request.GET.get('grade', '')
    section = request.GET.get('section', '')
    status = request.GET.get('status', '')
    pending_only = request.GET.get('pending_only') == '1'   # <-- new
    page_number = request.GET.get('page', 1)
    with schema_context(schema_name):
        students = Student.objects.all()
        if query:
            students = students.filter(
                Q(name__icontains=query) | Q(roll_number__icontains=query) |
                Q(father_name__icontains=query) | Q(father_cnic__icontains=query) |
                Q(parent_mobile__icontains=query) | Q(grade__icontains=query)
            )
        if grade:
            students = students.filter(grade=grade)
        if section:
            students = students.filter(section=section)
        if status:
            students = students.filter(status=status)
        students = students.order_by('-enrolled_on')
        
        # Compute pending for all students first
        student_list = []
        for s in students:
            s.pending_amount = get_overall_pending(s)
            student_list.append(s)
        
        # Apply pending_only filter if needed
        if pending_only:
            student_list = [s for s in student_list if s.pending_amount > 0]
        
        total_pending_all = sum(s.pending_amount for s in student_list)
        
        # Paginate the filtered list
        paginator = Paginator(student_list, 20)
        page_obj = paginator.get_page(page_number)
        
        grades = list(Student.objects.values_list('grade', flat=True).distinct().order_by('grade'))
        sections = list(Student.objects.values_list('section', flat=True).distinct().order_by('section'))
        status_choices = Student.STATUS_CHOICES
        total_active = Student.objects.filter(status='active').count()
        
    return {
        'tenant': tenant,
        'students': page_obj,
        'grades': grades,
        'sections': sections,
        'status_choices': status_choices,
        'search_query': query,
        'total_pending_all': total_pending_all,
        'total_active': total_active,
        'logo_url': tenant.school_logo.url if tenant.school_logo else None,
    }


def student_list(request, schema_name):
    if is_mobile_user_agent(request):
        return redirect('mobile_student_list', schema_name=schema_name)
    context = get_student_list_context(request, schema_name)
    return render(request, 'tenant/student_list.html', context)



@require_tenant_type(['school'])
@require_school_feature('students')
def mobile_student_list(request, schema_name):
    context = get_student_list_context(request, schema_name)
    return render(request, 'mobile/student_list.html', context)



def get_student_profile_context(request, schema_name, student_id):
    tenant = get_tenant(request, schema_name)
    page = request.GET.get('page', 1)
    search_date = request.GET.get('date', '').strip()
    with schema_context(schema_name):
        student = get_object_or_404(Student, id=student_id)
        today = date.today()
        current_month = today.month
        current_year = today.year

        fee_records_qs = student.fee_records.all().order_by('-year', '-month')
        total_fee = Decimal('0')

        for fr in fee_records_qs:
            total_fee += fr.total_amount
        fee_records = list(fee_records_qs)

        payments_qs_all = student.payments.all().order_by('payment_date')
        if search_date:
            try:
                parsed = datetime.strptime(search_date, '%Y-%m-%d').date()
                payments_qs_all = payments_qs_all.filter(payment_date=parsed)
            except ValueError:
                pass

        total_items_cost_all = Decimal('0')
        items_cost_per_payment = {}
        for p in payments_qs_all:
            items = _extract_item_sales_from_remarks(p.remarks or '')
            cost = sum(item['line_total'] for item in items)
            items_cost_per_payment[p.id] = cost
            total_items_cost_all += cost

        cumulative_fee_paid = Decimal('0')
        cumulative_items_paid = Decimal('0')
        payment_list = []

        for p in payments_qs_all:
            fee_paid = sum(fr.paid_amount for fr in p.fee_records.all())
            items_cost = items_cost_per_payment.get(p.id, Decimal('0'))
            items_paid = p.amount - fee_paid
            total_due_before = (total_fee - cumulative_fee_paid) + (total_items_cost_all - cumulative_items_paid)

            cumulative_fee_paid += fee_paid
            cumulative_items_paid += items_paid

            remaining_balance = (total_fee - cumulative_fee_paid) + (total_items_cost_all - cumulative_items_paid)
            if remaining_balance < 0:
                remaining_balance = Decimal('0')

            has_fee = p.fee_records.exists()
            remarks = (p.remarks or '').lower()
            has_items = 'items sold' in remarks
            if has_fee and has_items:
                p_type = 'Fee & Items'
            elif has_fee:
                p_type = 'Fee'
            elif has_items:
                p_type = 'Items'
            else:
                p_type = 'Unknown'
            p.payment_type_display = p_type

            payment_list.append({
                'payment': p,
                'fee_paid': fee_paid,
                'total_due_before': total_due_before,
                'remaining_balance': remaining_balance,
            })

        payment_list.reverse()
        paginator = Paginator(payment_list, 10)
        page_obj = paginator.get_page(page)

        total_paid = student.payments.aggregate(Sum('amount'))['amount__sum'] or 0
        fee_paid_total = sum(fr.paid_amount for fr in fee_records)
        item_purchase_total = total_paid - fee_paid_total
        pending_total = total_fee + total_items_cost_all - total_paid

        return {
            'tenant': tenant,
            'student': student,
            'fee_records': fee_records,
            'payments': page_obj,
            'total_fee': total_fee,
            'total_paid': total_paid,
            'pending_total': pending_total,
            'item_purchase_total': item_purchase_total,
            'current_month': current_month,
            'current_year': current_year,
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
            'search_date': search_date,
        }



@require_tenant_type(['school'])
@require_school_feature('students')
def student_profile(request, schema_name, student_id):
    if is_mobile_user_agent(request):
        return redirect('mobile_student_profile', schema_name=schema_name, student_id=student_id)
    context = get_student_profile_context(request, schema_name, student_id)
    return render(request, 'tenant/student_profile.html', context)



@require_tenant_type(['school'])
@require_school_feature('students')
def mobile_student_profile(request, schema_name, student_id):
    context = get_student_profile_context(request, schema_name, student_id)
    return render(request, 'mobile/student_profile.html', context)



@require_tenant_type(['school'])
@require_school_feature('fee_collection')
def mobile_fee_receipt(request, schema_name, receipt_id):
    return fee_receipt(request, schema_name, receipt_id, force_mobile=True)



@require_tenant_type(['school'])
def fee_receipt(request, schema_name, receipt_id, force_mobile=False):
    if is_mobile_user_agent(request) and not force_mobile:
        return redirect('mobile_fee_receipt', schema_name=schema_name, receipt_id=receipt_id)
    tenant = get_tenant(request, schema_name)
    with schema_context(schema_name):
        payment = get_object_or_404(PaymentTransaction.objects.select_related('student'), id=receipt_id)
        fee_records = list(payment.fee_records.all())
        item_details = _extract_item_sales_from_remarks(payment.remarks or '')
        
        # ---- IMPROVED: total pending fee before payment = sum of amounts of linked fee records ----
        total_pending_before = sum(fr.amount for fr in fee_records)
        total_items_cost = sum(item['line_total'] for item in item_details) if item_details else Decimal('0')
        total_paid = payment.amount
        # Remaining after this payment = total_pending_before + total_items_cost - total_paid
        remaining = (total_pending_before + total_items_cost) - total_paid
        if remaining < 0:
            remaining = Decimal('0')
        
        context = {
            'tenant': tenant,
            'payment': payment,
            'fee_records': fee_records,
            'item_details': item_details,
            'has_fee': bool(fee_records),
            'has_items': bool(item_details),
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
            # summary
            'total_fee_paid': total_pending_before,  # this is the fee amount covered by this payment
            'total_items_cost': total_items_cost,
            'total_paid': total_paid,
            'total_pending_before': total_pending_before,
            'remaining': remaining,
            'payment_mode_display': payment.get_payment_mode_display(),
            'payment_type_display': payment.payment_type,
        }
    template = 'mobile/receipt.html' if is_mobile_user_agent(request) else 'tenant/receipt.html'
    return render(request, template, context)



@require_tenant_type(['school'])
@require_school_feature('defaulters')
def defaulters(request, schema_name, force_mobile=False):
    """Defaulters list with search, filters, pagination, and analytics KPIs.
       Now includes students with overall pending (fee + items) even if no pending fee record.
    """
    tenant = get_tenant(request, schema_name)
    
    # Get query parameters
    q = request.GET.get('q', '').strip()
    grade = request.GET.get('grade', '')
    section = request.GET.get('section', '')
    days = request.GET.get('days', '0')
    sort_by = request.GET.get('sort_by', 'overdue')  # overdue, pending, name
    page_number = request.GET.get('page', 1)
    
    try:
        days = int(days)
    except:
        days = 0
    if days < 0:
        days = 0

    with schema_context(schema_name):
        today = timezone.localdate()
        cutoff = today - timedelta(days=days) if days > 0 else None
        
        # Start with all students (filter by search/grade/section if provided)
        students_qs = Student.objects.all()
        
        # Apply search
        if q:
            students_qs = students_qs.filter(
                Q(name__icontains=q) |
                Q(roll_number__icontains=q) |
                Q(father_name__icontains=q) |
                Q(father_cnic__icontains=q) |
                Q(parent_mobile__icontains=q)
            )
        # Apply grade filter
        if grade:
            students_qs = students_qs.filter(grade=grade)
        # Apply section filter
        if section:
            students_qs = students_qs.filter(section=section)
        
        # Build result list with computed fields
        result = []
        show_only_pending = request.GET.get('pending_only') == '1'
        for student in students_qs:
            overall_pending = get_overall_pending(student)
            if show_only_pending and overall_pending <= 0:
                continue
            if overall_pending <= 0:
                continue  # skip students with zero overall pending
            
            # Compute fee-only pending (still needed for display)
            fee_pending = sum(fr.remaining for fr in student.fee_records.filter(status__in=['pending','partial','overdue']))
            
            # Determine oldest due date among pending fee records (for overdue days)
            oldest_due = student.fee_records.filter(status__in=['pending', 'partial', 'overdue']).order_by('due_date').first()
            days_overdue = (today - oldest_due.due_date).days if oldest_due and oldest_due.due_date < today else 0
            
            result.append({
                'student': student,
                'pending_amount': overall_pending,
                'fee_pending': fee_pending,
                'days_overdue': days_overdue
            })
        
        # Sorting
        if sort_by == 'pending':
            result.sort(key=lambda x: x['pending_amount'], reverse=True)
        elif sort_by == 'name':
            result.sort(key=lambda x: x['student'].name.lower())
        else:  # overdue (default)
            result.sort(key=lambda x: x['days_overdue'], reverse=True)
        
        # --- Analytics KPIs ---
        total_defaulters = len(result)
        total_pending_all = sum(r['pending_amount'] for r in result)
        avg_overdue = sum(r['days_overdue'] for r in result) / total_defaulters if total_defaulters > 0 else 0
        max_overdue = max((r['days_overdue'] for r in result), default=0)
        
        # --- Pagination (15 per page) ---
        paginator = Paginator(result, 15)
        page_obj = paginator.get_page(page_number)
        
        # --- Distinct grades & sections for filter dropdowns ---
        grades = list(Student.objects.values_list('grade', flat=True).distinct().order_by('grade'))
        sections = list(Student.objects.values_list('section', flat=True).distinct().order_by('section'))
    
    context = {
        'tenant': tenant,
        'defaulters': page_obj,                    # paginated list
        'total_defaulters': total_defaulters,
        'total_pending_all': total_pending_all,
        'avg_overdue': round(avg_overdue, 1),
        'max_overdue': max_overdue,
        'days': days,
        'search_query': q,
        'grade_filter': grade,
        'section_filter': section,
        'sort_by': sort_by,
        'grades': grades,
        'sections': sections,
        'logo_url': tenant.school_logo.url if tenant.school_logo else None,
    }
    template = 'mobile/defaulters.html' if force_mobile else 'tenant/defaulters.html'
    return render(request, template, context)



@require_tenant_type(['school'])
@require_school_feature('reports')
def reports(request, schema_name, force_mobile=False):
    tenant = get_tenant(request, schema_name)
    report_type = request.GET.get('type', 'collection')
    today = timezone.localdate()
    quick_filter = request.GET.get('quick_filter')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    search_q = request.GET.get('search', '').strip()
    page_num = request.GET.get('page', 1)

    # Determine date range
    if quick_filter == 'today':
        start_date = end_date = today
    elif quick_filter == 'week':
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
    elif quick_filter == 'month':
        start_date = today.replace(day=1)
        end_date = today
    elif quick_filter == 'year':
        start_date = today.replace(month=1, day=1)
        end_date = today
    elif quick_filter == 'all':
        start_date = date(2000, 1, 1)
        end_date = today
    elif quick_filter == 'last6months':
        start_date = today - timedelta(days=180)
        end_date = today
    elif start_date_str and end_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            end_date = date.fromisoformat(end_date_str)
            if start_date > end_date:
                start_date, end_date = end_date, start_date
            quick_filter = 'custom'
        except:
            start_date = today - timedelta(days=180)
            end_date = today
            quick_filter = 'last6months'
    else:
        start_date = date(2000, 1, 1)
        end_date = today
        quick_filter = 'all'

    with schema_context(schema_name):
        payments_qs = PaymentTransaction.objects.filter(payment_date__gte=start_date, payment_date__lte=end_date)
        if search_q:
            payments_qs = payments_qs.filter(
                Q(receipt_number__icontains=search_q) |
                Q(student__name__icontains=search_q) |
                Q(student__roll_number__icontains=search_q)
            )

        paginator = Paginator(payments_qs.order_by('-payment_date'), 15)
        payments_page = paginator.get_page(page_num)

        total_collection = payments_qs.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        payment_count = payments_qs.count()

        pending_records = FeeRecord.objects.all()
        total_pending = sum(fr.remaining_total for fr in pending_records)

        total_collection_all = PaymentTransaction.objects.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        total_billed = total_collection_all + total_pending
        collection_rate = (float(total_collection_all) / float(total_billed) * 100) if total_billed > 0 else 0

        defaulters_count = Student.objects.filter(fee_records__status__in=['pending', 'partial', 'overdue']).distinct().count()

        monthly_data = []
        for i in range(5, -1, -1):
            m = today.month - i
            y = today.year
            if m <= 0:
                m += 12
                y -= 1
            total = PaymentTransaction.objects.filter(payment_date__year=y, payment_date__month=m).aggregate(Sum('amount'))['amount__sum'] or 0
            monthly_data.append({'month': f"{m}/{y}", 'amount': float(total)})

        mode_totals = {}
        for mode_code, mode_name in PaymentTransaction.PAYMENT_MODE_CHOICES:
            total = payments_qs.filter(payment_mode=mode_code).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
            if total > 0:
                mode_totals[mode_name] = float(total)
        mode_distribution = [{'name': k, 'amount': v} for k, v in mode_totals.items()]

        class_pending = []
        grades = Student.objects.values_list('grade', flat=True).distinct().order_by('grade')
        grades = list(grades)
        for grade in grades:
            students = Student.objects.filter(grade=grade)
            pending = sum(get_overall_pending(s) for s in students)
            if pending > 0:
                class_pending.append({'grade': grade, 'pending': float(pending)})
        class_pending.sort(key=lambda x: x['pending'], reverse=True)

        top_defaulters = []
        for student in Student.objects.all():
            pending = get_overall_pending(student)
            if pending > 0:
                top_defaulters.append({'student': student, 'pending': float(pending)})
        top_defaulters = sorted(top_defaulters, key=lambda x: x['pending'], reverse=True)[:5]

        defaulters_list = Student.objects.filter(fee_records__status__in=['pending', 'partial', 'overdue']).distinct()
        defaulters_data = []
        for student in defaulters_list:
            pending = get_overall_pending(student)
            oldest_due = student.fee_records.filter(status__in=['pending', 'partial', 'overdue']).order_by('due_date').first()
            days_overdue = (timezone.localdate() - oldest_due.due_date).days if oldest_due and oldest_due.due_date < timezone.localdate() else 0
            defaulters_data.append({
                'student': student,
                'pending_amount': pending,
                'days_overdue': days_overdue
            })
        defaulters_data.sort(key=lambda x: x['days_overdue'], reverse=True)

        context = {
            'tenant': tenant,
            'report_type': report_type,
            'start_date': start_date,
            'end_date': end_date,
            'quick_filter': quick_filter,
            'search_query': search_q,
            'total_collection': total_collection,
            'total_pending': total_pending,
            'collection_rate': round(collection_rate, 1),
            'defaulters_count': defaulters_count,
            'monthly_data': monthly_data,
            'mode_distribution': mode_distribution,
            'class_pending': class_pending,
            'top_defaulters': top_defaulters,
            'defaulters_data': defaulters_data,
            'payments': payments_page,
            'total': total_collection,
            'payment_count': payment_count,
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
            'total_collection_all': total_collection_all,
        }
        template = 'mobile/reports.html' if force_mobile else 'tenant/reports.html'
    return render(request, template, context)



@require_tenant_type(['school'])
@require_school_feature('fee_collection')
def fee_collection(request, schema_name, student_id=None, force_mobile=False):
    if is_mobile_user_agent(request) and not force_mobile:
        if student_id is not None:
            return redirect('mobile_fee_collection', schema_name=schema_name, student_id=student_id)
        return redirect('mobile_fee_collection', schema_name=schema_name)
    tenant = get_tenant(request, schema_name)
    mobile_mode = force_mobile or is_mobile_user_agent(request)
    with schema_context(schema_name):
        # Handle POST payment (works for both list and student views)
        if request.method == 'POST':
            student_id_post = request.POST.get('student_id')
            amount_raw = request.POST.get('amount')
            payment_mode = request.POST.get('payment_mode')
            remarks = request.POST.get('remarks', '')
            product_items_raw = request.POST.get('product_items_json', '[]')
            try:
                product_items = json.loads(product_items_raw or '[]')
            except Exception:
                product_items = []

            if student_id_post and amount_raw:
                try:
                    student = Student.objects.get(id=student_id_post)
                    amount = Decimal(amount_raw)

                    product_total = Decimal('0.00')
                    item_breakdown = []
                    for item in product_items:
                        try:
                            product_id = int(item.get('product_id'))
                            qty = int(item.get('quantity', 0))
                        except (TypeError, ValueError):
                            continue
                        if qty <= 0:
                            continue

                        product = Product.objects.filter(id=product_id).first()
                        if not product:
                            raise ValueError(f"Product {product_id} not found")
                        if product.quantity < qty:
                            raise ValueError(f"Only {product.quantity} units available for {product.name}")

                        line_total = product.selling_price * qty
                        product_total += line_total
                        item_breakdown.append((product, qty, line_total))

                    pending_records = student.fee_records.filter(status__in=['pending', 'partial', 'overdue']).order_by('due_date')
                    # Compute fee pending from fee records only (exclude items)
                    fee_pending = sum(r.remaining_total for r in pending_records)
                    total_due = fee_pending + product_total

                    amount_received = amount
                    # First, pay as much fee as possible
                    fee_to_apply = min(amount_received, Decimal(fee_pending)) if fee_pending else Decimal('0.00')
                    amount_left = amount_received - fee_to_apply
                    # Then, use remainder for items
                    item_to_apply = min(amount_left, product_total) if product_total else Decimal('0.00')

                    remaining = fee_to_apply
                    paid_records = []
                    for record in pending_records:
                        if remaining <= 0:
                            break
                        due = record.remaining_total
                        apply_now = min(due, remaining)
                        record.paid_amount += apply_now
                        remaining -= apply_now
                        record.save()
                        paid_records.append(record)

                    item_details = '; '.join([
                        f"{product.name} x{qty} @ ₹{product.selling_price} = ₹{line_total}"
                        for product, qty, line_total in item_breakdown
                    ]) if item_breakdown else ''

                    combined_remarks = (remarks or '').strip()
                    if fee_to_apply > 0 and item_breakdown:
                        combined_remarks = (combined_remarks + '\n' if combined_remarks else '') + (
                            f"Fee payment applied: ₹{fee_to_apply:.2f}. Items sold: {item_details}"
                        )
                    elif fee_to_apply > 0:
                        combined_remarks = combined_remarks or 'Fee payment'
                    elif item_breakdown:
                        combined_remarks = (combined_remarks + '\n' if combined_remarks else '') + ('Items sold: ' + item_details)

                    payment_record = None
                    if amount_received > 0:
                        payment_record = PaymentTransaction.objects.create(
                            student=student,
                            amount=amount_received,
                            payment_mode=payment_mode,
                            payment_type='full' if amount_received >= total_due else 'partial',
                            remarks=combined_remarks or 'Fee and item payment',
                            created_by=request.session.get('school_admin_username', 'admin')
                        )
                        if paid_records:
                            payment_record.fee_records.set(paid_records)

                        if item_breakdown:
                            for product, qty, _ in item_breakdown:
                                product.quantity -= qty
                                product.save(update_fields=['quantity'])

                    if amount_received > total_due:
                        messages.info(request, f'Payment received exceeds total due by ₹{(amount_received - total_due):.2f}.')
                    elif amount_received < total_due:
                        messages.info(request, f'Amount received covers pending fee and selected items partially. Remaining balance: ₹{(total_due - amount_received):.2f}.')

                    if payment_record:
                        messages.success(request, f"Payment recorded successfully. Receipt: {payment_record.receipt_number}")
                        return redirect('fee_receipt', schema_name=schema_name, receipt_id=payment_record.id)

                    messages.success(request, 'Payment recorded.')
                    return redirect('fee_collection', schema_name=schema_name, student_id=student.id)
                except Student.DoesNotExist:
                    messages.error(request, 'Student not found')
                except Exception as e:
                    messages.error(request, f'Error processing payment: {str(e)}')
            else:
                messages.error(request, 'Invalid payment data')
            return redirect('fee_collection', schema_name=schema_name)

        # ---------- GET request ----------
        # If student_id is provided, show the dedicated payment page for that student
        if student_id is not None:
            try:
                student = Student.objects.get(id=student_id)
                pending_records = student.fee_records.filter(status__in=['pending', 'partial', 'overdue']).order_by('due_date')
                total_pending = get_overall_pending(student)
                products = list(Product.objects.select_related('category').filter(quantity__gt=0).order_by('category__name', 'name'))
                categories = list(ProductCategory.objects.all().order_by('name'))
                context = {
                    'tenant': tenant,
                    'student': student,
                    'pending_records': pending_records,
                    'total_pending': total_pending,
                    'products': products,
                    'categories': categories,
                    'logo_url': tenant.school_logo.url if tenant.school_logo else None,
                }
                template_name = 'mobile/collect_fee.html' if mobile_mode else 'tenant/collect_fee.html'
                return render(request, template_name, context)
            except Student.DoesNotExist:
                messages.error(request, "Student not found")
                return redirect('fee_collection', schema_name=schema_name)

        # Otherwise, show the list of students with pending fees (no inline section)
        search_filter = request.GET.get('pending_search', '')
        grade_filter = request.GET.get('pending_grade', '')
        section_filter = request.GET.get('pending_section', '')
        page_number = request.GET.get('page', 1)

        students_qs = Student.objects.all()
        if search_filter:
            students_qs = students_qs.filter(
                Q(name__icontains=search_filter) | Q(roll_number__icontains=search_filter) |
                Q(father_name__icontains=search_filter) | Q(father_cnic__icontains=search_filter)
            )
        if grade_filter:
            students_qs = students_qs.filter(grade=grade_filter)
        if section_filter:
            students_qs = students_qs.filter(section=section_filter)

        pending_students = []
        for s in students_qs:
            pending = get_overall_pending(s)
            if pending > 0:
                s.pending_total = pending
                pending_students.append(s)
        pending_students.sort(key=lambda x: x.pending_total, reverse=True)

        paginator = Paginator(pending_students, 20)
        pending_page = paginator.get_page(page_number)

        total_pending_all = sum(get_overall_pending(s) for s in Student.objects.all())
        total_payments_count = PaymentTransaction.objects.count()
        today = date.today()
        today_collection = PaymentTransaction.objects.filter(payment_date=today).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        recent_payments = list(PaymentTransaction.objects.select_related('student').order_by('-payment_date')[:5])
        grades = list(Student.objects.values_list('grade', flat=True).distinct().order_by('grade'))
        sections = list(Student.objects.values_list('section', flat=True).distinct().order_by('section'))
        products = list(Product.objects.select_related('category').filter(quantity__gt=0).order_by('category__name', 'name'))
        categories = list(ProductCategory.objects.all().order_by('name'))

        context = {
            'tenant': tenant,
            'pending_students': pending_page,
            'recent_payments': recent_payments,
            'total_pending_all': total_pending_all,
            'total_payments_count': total_payments_count,
            'today_collection': today_collection,
            'grades': grades,
            'sections': sections,
            'products': products,
            'categories': categories,
            'search_filter': search_filter,
            'grade_filter': grade_filter,
            'section_filter': section_filter,
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        }
        template_name = 'mobile/fee_collection.html' if mobile_mode else 'tenant/fee_collection.html'
        return render(request, template_name, context)


@csrf_exempt
@require_http_methods(["GET"])
def debug_payments_api(request):
    if not request.session.get('school_admin_authenticated'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    schema_name = request.session.get('school_admin_schema')
    if not schema_name:
        return JsonResponse({'error': 'No tenant schema'}, status=400)
    try:
        tenant = SchoolClient.objects.get(schema_name=schema_name)
    except SchoolClient.DoesNotExist:
        return JsonResponse({'error': 'Tenant not found'}, status=404)
    with schema_context(schema_name):
        payments = PaymentTransaction.objects.all().order_by('-payment_date')[:10]
        data = [{
            'id': p.id,
            'receipt_number': p.receipt_number,
            'student_id': p.student_id,
            'amount': float(p.amount),
            'date': p.payment_date.strftime('%Y-%m-%d')
        } for p in payments]
        return JsonResponse({'payments': data, 'total': PaymentTransaction.objects.count()})

# ------------------- Settings -------------------


@require_tenant_type(['school'])
def settings(request, schema_name):
    tenant = get_tenant(request, schema_name)
    if request.method == 'POST':
        school_name = request.POST.get('school_name', '').strip()
        if school_name:
            tenant.name = school_name
        admin_username = request.POST.get('admin_username', '').strip()
        admin_password = request.POST.get('admin_password', '')
        admin_password_confirm = request.POST.get('admin_password_confirm', '')
        if admin_username:
            tenant.admin_username = admin_username
        if admin_password:
            if admin_password == admin_password_confirm:
                tenant.admin_password = admin_password
            else:
                messages.error(request, "Passwords do not match.")
                return redirect('settings', schema_name=schema_name)
        if request.FILES.get('school_logo'):
            tenant.school_logo = request.FILES['school_logo']
        tenant.save()
        messages.success(request, "Settings updated successfully.")
        return redirect('settings', schema_name=schema_name)
    context = {'tenant': tenant, 'logo_url': tenant.school_logo.url if tenant.school_logo else None}
    return render(request, 'tenant/settings.html', context)

# ------------------- Fee Structure -------------------


@require_tenant_type(['school'])
@require_school_feature('fee_structure')
def fee_structure(request, schema_name):
    if is_mobile_user_agent(request):
        return redirect('mobile_fee_structure', schema_name=schema_name)
    tenant = get_tenant(request, schema_name)
    edit_grade = request.GET.get('edit', '')
    with schema_context(schema_name):
        if request.method == 'POST':
            grade = request.POST.get('grade')
            monthly_fee = request.POST.get('monthly_fee')
            if grade and monthly_fee:
                obj, created = FeeStructure.objects.update_or_create(
                    grade=grade,
                    defaults={'monthly_fee': monthly_fee}
                )
                Student.objects.filter(grade=grade).update(custom_fee=monthly_fee)
                messages.success(request, f"Fee structure for {grade} saved successfully.")
            else:
                messages.error(request, "Please provide both grade and monthly fee.")
            return redirect('fee_structure', schema_name=schema_name)

        # CRITICAL FIX: evaluate queryset inside schema_context (convert to list)
        structures = list(FeeStructure.objects.all().order_by('grade'))

        # Compute stats
        total_structures = len(structures)
        if structures:
            fees = [fs.monthly_fee for fs in structures]
            avg_fee = sum(fees) / len(fees)
            min_fee = min(fees)
            max_fee = max(fees)
        else:
            avg_fee = min_fee = max_fee = 0

        print(f"[DEBUG] Tenant {schema_name}: found {len(structures)} fee structure(s)")
        for fs in structures:
            print(f"  - {fs.grade}: ₹{fs.monthly_fee}")

        form = FeeStructureForm()
        if edit_grade:
            try:
                edit_obj = FeeStructure.objects.get(grade=edit_grade)
                form = FeeStructureForm(initial={'grade': edit_obj.grade, 'monthly_fee': edit_obj.monthly_fee})
            except FeeStructure.DoesNotExist:
                pass

    context = {
        'tenant': tenant,
        'form': form,
        'fee_structures': structures,
        'edit_grade': edit_grade,
        'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        'debug_count': len(structures),
        'total_structures': total_structures,
        'avg_fee': avg_fee,
        'min_fee': min_fee,
        'max_fee': max_fee,
    }
    return render(request, 'tenant/fee_structure.html', context)
# ------------------- Fee Settings -------------------



@require_tenant_type(['school'])
@require_school_feature('fee_structure')
def mobile_fee_structure(request, schema_name):
    """Mobile version of fee structure page."""
    tenant = get_tenant(request, schema_name)
    edit_grade = request.GET.get('edit', '')
    with schema_context(schema_name):
        if request.method == 'POST':
            grade = request.POST.get('grade')
            monthly_fee = request.POST.get('monthly_fee')
            if grade and monthly_fee:
                obj, created = FeeStructure.objects.update_or_create(
                    grade=grade,
                    defaults={'monthly_fee': monthly_fee}
                )
                Student.objects.filter(grade=grade).update(custom_fee=monthly_fee)
                messages.success(request, f"Fee structure for {grade} saved successfully.")
            else:
                messages.error(request, "Please provide both grade and monthly fee.")
            return redirect('mobile_fee_structure', schema_name=schema_name)

        structures = list(FeeStructure.objects.all().order_by('grade'))

        # Compute stats
        total_structures = len(structures)
        if structures:
            fees = [fs.monthly_fee for fs in structures]
            avg_fee = sum(fees) / len(fees)
            min_fee = min(fees)
            max_fee = max(fees)
        else:
            avg_fee = min_fee = max_fee = 0

        form = FeeStructureForm()
        if edit_grade:
            try:
                edit_obj = FeeStructure.objects.get(grade=edit_grade)
                form = FeeStructureForm(initial={'grade': edit_obj.grade, 'monthly_fee': edit_obj.monthly_fee})
            except FeeStructure.DoesNotExist:
                pass

    context = {
        'tenant': tenant,
        'form': form,
        'fee_structures': structures,
        'edit_grade': edit_grade,
        'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        'debug_count': len(structures),
        'total_structures': total_structures,
        'avg_fee': avg_fee,
        'min_fee': min_fee,
        'max_fee': max_fee,
    }
    return render(request, 'mobile/fee_structure.html', context)



@require_tenant_type(['school'])
@require_school_feature('fee_settings')
def fee_settings(request, schema_name, force_mobile=False):
    """Fee settings page with automation controls and extra charges."""
    tenant = get_tenant(request, schema_name)
    with schema_context(schema_name):
        settings, created = SchoolFeeSettings.objects.get_or_create(pk=1)

        # Handle POST (save settings or toggle automation)
        if request.method == 'POST':
            # Check if this is a toggle action
            if 'toggle_automation' in request.POST:
                settings.automation_enabled = not settings.automation_enabled
                settings.save(update_fields=['automation_enabled'])
                messages.success(request, f"Automation {'enabled' if settings.automation_enabled else 'disabled'}.")
                return redirect('fee_settings', schema_name=schema_name)

            # Otherwise, save settings form
            form = FeeSettingsForm(request.POST, instance=settings)
            if form.is_valid():
                # Save the form (includes fee_generation_day, due_date_offset, late_fee_penalty)
                form.save()

                # Handle extra charges (JSON)
                extra_charges_json = request.POST.get('extra_charges_json', '[]')
                try:
                    import json
                    extra_charges = json.loads(extra_charges_json)
                    if isinstance(extra_charges, list):
                        settings.default_extra_charges = extra_charges
                        settings.save(update_fields=['default_extra_charges'])
                except Exception as e:
                    messages.error(request, f"Error saving extra charges: {e}")

                messages.success(request, "Fee settings updated successfully.")
                return redirect('fee_settings', schema_name=schema_name)
        else:
            form = FeeSettingsForm(instance=settings)

        # Compute total extra charges for display
        extra_charges = settings.default_extra_charges or []
        total_extra = sum((ch.get('amount', 0) for ch in extra_charges), 0)

        context = {
            'tenant': tenant,
            'form': form,
            'settings': settings,
            'extra_charges': extra_charges,
            'total_extra': total_extra,
            'automation_enabled': settings.automation_enabled,
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        }
        template = 'mobile/fee_settings.html' if force_mobile else 'tenant/fee_settings.html'
        return render(request, template, context)



def family_payment(request, schema_name):
    tenant = get_tenant(request, schema_name)
    with schema_context(schema_name):
        if request.method == 'POST':
            form = FamilyPaymentForm(request.POST)
            if form.is_valid():
                cnic = form.cleaned_data['father_cnic']
                amount = form.cleaned_data['amount'] or None
                mode = form.cleaned_data['payment_mode']
                remarks = form.cleaned_data['remarks']
                students = Student.objects.filter(father_cnic=cnic, status='active')
                if not students.exists():
                    messages.error(request, "No student found with this CNIC.")
                    return redirect('family_payment', schema_name=schema_name)
                total_pending = 0
                all_pending_records = []
                for s in students:
                    records = s.fee_records.filter(status__in=['pending', 'partial', 'overdue']).order_by('due_date')
                    total_pending += sum(r.remaining_total for r in records)
                    all_pending_records.extend(records)
                if amount is None:
                    amount = total_pending
                if amount > total_pending:
                    messages.error(request, f"Amount exceeds total pending ({total_pending})")
                    return redirect('family_payment', schema_name=schema_name)
                remaining = amount
                paid_records = []
                for record in all_pending_records:
                    if remaining <= 0:
                        break
                    due = record.remaining_total
                    if remaining >= due:
                        record.paid_amount = record.total_amount
                        remaining -= due
                    else:
                        record.paid_amount += remaining
                        remaining = 0
                    record.save()
                    paid_records.append(record)
                for student in students:
                    student_paid = [r for r in paid_records if r.student == student]
                    if student_paid:
                        payment = PaymentTransaction.objects.create(
                            student=student, amount=sum(r.paid_amount for r in student_paid),
                            payment_mode=mode, payment_type='full' if remaining == 0 else 'partial',
                            remarks=remarks, created_by=request.session.get('school_admin_username', 'admin')
                        )
                        payment.fee_records.set(student_paid)
                messages.success(request, f"Family payment of ₹{amount} processed for CNIC {cnic}")
                return redirect('reports', schema_name=schema_name)
        else:
            form = FamilyPaymentForm()
    context = {'tenant': tenant, 'form': form, 'logo_url': tenant.school_logo.url if tenant.school_logo else None}
    return render(request, 'tenant/family_payment.html', context)

# ------------------- API: Student Search -------------------


@require_tenant_type(['school'])
@require_school_feature('students')
def student_search_api(request, schema_name):
    q = request.GET.get("q", "")
    with schema_context(schema_name):
        students = Student.objects.filter(
            Q(name__icontains=q) | Q(roll_number__icontains=q) | Q(father_name__icontains=q) | Q(father_cnic__icontains=q)
        )[:5]
        data = [{"id": s.id, "name": s.name, "roll_no": s.roll_number, "grade": s.grade} for s in students]
    return JsonResponse(data, safe=False)


# ------------------- Add Student -------------------


@require_tenant_type(['school'])
@require_school_feature('students')
def add_student(request, schema_name):
    tenant = get_tenant(request, schema_name)
    with schema_context(schema_name):
        if request.method == "POST":
            student, form = create_student_from_payload(schema_name, request.POST)
            if student:
                messages.success(request, f"Student {student.name} added successfully. Roll No: {student.roll_number}")
                return redirect("student_list", schema_name=schema_name)
        else:
            form = StudentForm()
        grades = FeeStructure.objects.values_list("grade", flat=True).distinct()
        context = {
            "tenant": tenant, "form": form, "grades": grades,
            "logo_url": tenant.school_logo.url if tenant.school_logo else None,
        }
    return render(request, "tenant/student_form.html", context)


@csrf_exempt
@require_http_methods(["POST"])
@require_tenant_type(['school'])
@require_school_feature('students')
def sync_offline_student_api(request, schema_name):
    get_tenant(request, schema_name)
    try:
        if request.content_type and 'application/json' in request.content_type:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        else:
            payload = request.POST
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'errors': ['Invalid JSON payload']}, status=400)

    action = payload.get('action', 'create')
    student = None
    form = None

    if action == 'edit':
        student_id = payload.get('student_id')
        if not student_id:
            return JsonResponse({'ok': False, 'errors': ['Missing student_id for edit action']}, status=400)
        student, form = update_student_from_payload(schema_name, student_id, payload)
    else:
        student, form = create_student_from_payload(schema_name, payload)

    if student:
        return JsonResponse({'ok': True, 'student_id': student.id, 'roll_number': student.roll_number})

    return JsonResponse({'ok': False, 'errors': json.loads(form.errors.as_json())}, status=400)


@csrf_exempt
@require_http_methods(["POST"])
@require_tenant_type(['school'])
@require_school_feature('fee_collection')
def sync_offline_payment_api(request, schema_name):
    get_tenant(request, schema_name)
    try:
        if request.content_type and 'application/json' in request.content_type:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        else:
            payload = request.POST
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON payload'}, status=400)

    student_id = payload.get('student_id')
    if not student_id:
        return JsonResponse({'ok': False, 'error': 'Missing student_id'}, status=400)

    try:
        amount = Decimal(str(payload.get('amount', '0')))
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid amount'}, status=400)

    if amount <= 0:
        return JsonResponse({'ok': False, 'error': 'Amount must be greater than zero'}, status=400)

    payment_mode = payload.get('payment_mode') or 'cash'
    remarks = payload.get('remarks') or ''
    temp_receipt = payload.get('temp_receipt')
    product_items = payload.get('product_items') or []

    if not temp_receipt:
        temp_receipt = f"OFFLINE-{int(datetime.utcnow().timestamp() * 1000)}"

    try:
        with schema_context(schema_name):
            student = Student.objects.filter(id=student_id).first()
            if not student:
                return JsonResponse({'ok': False, 'error': 'Student not found'}, status=404)

            existing = PaymentTransaction.objects.filter(remarks__icontains=temp_receipt).first()
            if existing:
                return JsonResponse({'ok': True, 'receipt_number': existing.receipt_number, 'payment_id': existing.id})

            payment_record, total_due = apply_fee_payment(
                student,
                amount,
                payment_mode,
                remarks,
                product_items,
                created_by=request.session.get('school_admin_username', 'admin')
            )
            if payment_record:
                payment_record.remarks = build_payment_remarks(remarks, min(amount, total_due), [
                    (Product.objects.get(id=int(item.get('product_id'))), int(item.get('quantity', 0)), Product.objects.get(id=int(item.get('product_id'))).selling_price * int(item.get('quantity', 0)))
                    for item in product_items if item.get('product_id') and int(item.get('quantity', 0)) > 0
                ], temp_receipt=temp_receipt)
                payment_record.save(update_fields=['remarks'])

            return JsonResponse({'ok': True, 'receipt_number': payment_record.receipt_number, 'payment_id': payment_record.id})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)


@require_tenant_type(['school'])
@require_school_feature('students')
def add_student_mobile(request, schema_name):
    tenant = get_tenant(request, schema_name)
    with schema_context(schema_name):
        if request.method == "POST":
            student, form = create_student_from_payload(schema_name, request.POST)
            if student:
                messages.success(request, f"Student {student.name} added successfully. Roll No: {student.roll_number}")
                return redirect("mobile_student_list", schema_name=schema_name)
        else:
            form = StudentForm()
        grades = FeeStructure.objects.values_list("grade", flat=True).distinct()
        context = {
            "tenant": tenant,
            "form": form,
            "grades": grades,
            "logo_url": tenant.school_logo.url if tenant.school_logo else None,
        }
    return render(request, "mobile/student_form.html", context)

# ------------------- Edit Student -------------------


@require_tenant_type(['school'])
@require_school_feature('students')
def edit_student(request, schema_name, student_id):
    tenant = get_tenant(request, schema_name)
    with schema_context(schema_name):
        student = get_object_or_404(Student, id=student_id)
        if request.method == "POST":
            form = StudentForm(request.POST, instance=student)
            if form.is_valid():
                form.save()
                messages.success(request, f"Student {student.name} updated successfully.")
                # Redirect to mobile profile if mobile user agent
                if is_mobile_user_agent(request):
                    return redirect('mobile_student_profile', schema_name=schema_name, student_id=student.id)
                return redirect("student_list", schema_name=schema_name)
        else:
            form = StudentForm(instance=student)
        grades = FeeStructure.objects.values_list("grade", flat=True).distinct()
        context = {
            "tenant": tenant, "form": form, "student": student, "grades": grades,
            "logo_url": tenant.school_logo.url if tenant.school_logo else None,
        }
    return render(request, "tenant/student_form.html", context)


@csrf_exempt
@require_http_methods(["GET"])
def fee_status_api(request):
    if not request.session.get("school_admin_authenticated"):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    schema_name = request.session.get("school_admin_schema")
    if not schema_name:
        return JsonResponse({"error": "No tenant schema"}, status=400)
    try:
        tenant = SchoolClient.objects.get(schema_name=schema_name)
    except SchoolClient.DoesNotExist:
        return JsonResponse({"error": "Tenant not found"}, status=404)
    with schema_context(schema_name):
        settings, _ = SchoolFeeSettings.objects.get_or_create(pk=1)
        last_record = FeeRecord.objects.order_by("-year", "-month").first()
        last_gen = f"{last_record.month}/{last_record.year}" if last_record else "Never"
        today = timezone.localdate()
        gen_day = settings.fee_generation_day
        from calendar import monthrange
        if today.day <= gen_day:
            next_date = date(today.year, today.month, min(gen_day, monthrange(today.year, today.month)[1]))
        else:
            next_month = today.month + 1 if today.month < 12 else 1
            next_year = today.year + 1 if today.month == 12 else today.year
            next_date = date(next_year, next_month, min(gen_day, monthrange(next_year, next_month)[1]))
        return JsonResponse({"last_generation": last_gen, "next_generation": next_date.strftime("%Y-%m-%d"), "status": "success"})


# ------------------- API: Manual Generate (All Students) -------------------


@csrf_exempt
@require_http_methods(["POST"])
def manual_generate_api(request):
    """Generate fee records for all active students with extra charges."""
    if not request.session.get('school_admin_authenticated'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    schema_name = request.session.get('school_admin_schema')
    if not schema_name:
        return JsonResponse({'error': 'No tenant schema'}, status=400)
    try:
        tenant = SchoolClient.objects.get(schema_name=schema_name)
    except SchoolClient.DoesNotExist:
        return JsonResponse({'error': 'Tenant not found'}, status=404)

    with schema_context(schema_name):
        settings, _ = SchoolFeeSettings.objects.get_or_create(pk=1)
        today = timezone.localdate()
        month = today.month
        year = today.year
        students = Student.objects.filter(status='active')
        if not students.exists():
            return JsonResponse({'message': 'No active students found.'})

        due_date = today + timedelta(days=settings.due_date_offset)

        # Compute extra charges sum
        extra_charges = settings.default_extra_charges or []
        total_extra = sum((ch.get('amount', 0) for ch in extra_charges), 0)

        created = 0
        skipped_existing = 0
        skipped_no_fee = 0

        for student in students:
            # Check if already has fee record for this month
            existing = FeeRecord.objects.filter(student=student, month=month, year=year).first()
            if existing:
                skipped_existing += 1
                continue

            # PRIORITY: class fee structure FIRST, then custom fee
            fee_struct = FeeStructure.objects.filter(grade=student.grade).first()
            if fee_struct:
                base_fee = fee_struct.monthly_fee
                # Update custom_fee to match the structure for consistency
                if student.custom_fee != base_fee:
                    student.custom_fee = base_fee
                    student.save(update_fields=['custom_fee'])
            else:
                base_fee = student.custom_fee if student.custom_fee > 0 else 0

            if base_fee > 0:
                total_fee = base_fee  # extra charges stored separately in extra_charges field
                fee_record = FeeRecord.objects.create(
                    student=student, month=month, year=year,
                    amount=total_fee, due_date=due_date, status='pending',
                    extra_charges=extra_charges  # store extra charges on the record
                )
                created += 1
            else:
                skipped_no_fee += 1

        # Create a log entry
        ManualGenerationLog.objects.create(
            month=month,
            year=year,
            created_count=created,
            skipped_existing=skipped_existing,
            skipped_no_fee=skipped_no_fee,
            triggered_by=request.session.get('school_admin_username', 'admin'),
            log_type='manual'
        )

        # Create notification
        create_fee_generation_notification(schema_name, month, year, created, request.session.get('school_admin_username', 'admin'), mobile=is_mobile_user_agent(request))


        message = f"Generated {created} fee records for {month}/{year}."
        if skipped_existing > 0:
            message += f" Skipped {skipped_existing} students because they already have a fee record."
        if skipped_no_fee > 0:
            message += f" Skipped {skipped_no_fee} students because no fee structure defined for their grade."
        return JsonResponse({'message': message, 'created': created, 'skipped_existing': skipped_existing, 'skipped_no_fee': skipped_no_fee})


def manual_generate_single_api(request):
    if not request.session.get("school_admin_authenticated"):
        return JsonResponse({"error": "Unauthorized"}, status=401)
    schema_name = request.session.get("school_admin_schema")
    if not schema_name:
        return JsonResponse({"error": "No tenant schema"}, status=400)
    student_id = request.GET.get("student_id") or request.POST.get("student_id")
    custom_amount = request.POST.get("custom_amount") or request.GET.get("custom_amount")
    if not student_id:
        return JsonResponse({"error": "Student ID required"}, status=400)
    print(f"[DEBUG] manual_generate_single_api called for student {student_id}, custom_amount={custom_amount}")
    try:
        tenant = SchoolClient.objects.get(schema_name=schema_name)
    except SchoolClient.DoesNotExist:
        return JsonResponse({"error": "Tenant not found"}, status=404)
    with schema_context(schema_name):
        try:
            student = Student.objects.get(id=student_id)
        except Student.DoesNotExist:
            return JsonResponse({"error": "Student not found"}, status=404)
        settings, _ = SchoolFeeSettings.objects.get_or_create(pk=1)
        today = timezone.localdate()
        month = today.month
        year = today.year
        
        existing_record = FeeRecord.objects.filter(student=student, month=month, year=year).first()
        
        # If record exists and has any paid amount, prevent modification
        if existing_record and existing_record.paid_amount > 0:
            return JsonResponse({
                "error": f"Fee already exists for {month}/{year} with paid amount ₹{existing_record.paid_amount}. Cannot modify."
            }, status=400)
        
        # Determine base fee amount
        if custom_amount:
            try:
                base_fee = Decimal(custom_amount)
                if base_fee <= 0:
                    raise ValueError
            except:
                return JsonResponse({"error": "Invalid custom amount"}, status=400)
            # If custom amount is provided, we treat it as the total (including charges)
            # We will not add extra charges automatically.
            extra_charges = []
            total_extra = Decimal('0')
            final_amount = base_fee
        else:
            # No custom amount: use student's custom_fee or grade fee structure
            base_fee = student.custom_fee if student.custom_fee > 0 else Decimal('0')
            if base_fee == 0:
                fee_struct = FeeStructure.objects.filter(grade=student.grade).first()
                if fee_struct:
                    base_fee = fee_struct.monthly_fee
                    student.custom_fee = base_fee
                    student.save(update_fields=["custom_fee"])
            if base_fee <= 0:
                return JsonResponse({"error": "No fee structure defined for this grade and no valid custom amount provided."}, status=400)
            
            # Get default extra charges from school settings
            extra_charges = settings.default_extra_charges or []
            # Keep base fee separate; extra charges stored separately
            final_amount = base_fee
        
        due_date = today + timedelta(days=settings.due_date_offset)
        
        if existing_record:
            # Update existing record with the new amount and extra charges
            existing_record.amount = final_amount
            existing_record.due_date = due_date
            existing_record.extra_charges = extra_charges
            existing_record.due_date_offset = settings.due_date_offset
            existing_record.late_fee_per_day = settings.late_fee_penalty
            existing_record.save()
            print(f"[DEBUG] Updated fee for {student.name} to ₹{final_amount} (base: {base_fee}, extras: {total_extra})")
            return JsonResponse({
                "message": f"Fee amount updated for {student.name} for {month}/{year} to ₹{final_amount} (including extras)."
            })
        else:
            FeeRecord.objects.create(
                student=student, month=month, year=year,
                amount=final_amount, due_date=due_date, status="pending",
                extra_charges=extra_charges,
                due_date_offset=settings.due_date_offset,
                late_fee_per_day=settings.late_fee_penalty
            )
            print(f"[DEBUG] Created fee for {student.name} with amount ₹{final_amount} (base: {base_fee}, extras: {total_extra})")
            return JsonResponse({
                "message": f"Fee record created for {student.name} for {month}/{year} with amount ₹{final_amount} (including extras)."
            })


def student_fee_records_api(request, schema_name, student_id):
    """API: Return JSON list of fee records for a student."""
    from django.http import JsonResponse
    from .models import Student
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        try:
            student = Student.objects.get(id=student_id)
        except Student.DoesNotExist:
            return JsonResponse({'error': 'Student not found'}, status=404)
        records = []
        for fr in student.fee_records.all().order_by('-year', '-month'):
            records.append({
                'id': fr.id,
                'month': fr.month,
                'year': fr.year,
                'amount': float(fr.total_amount),
                'paid_amount': float(fr.paid_amount),
                'status': fr.get_status_display(),
                'due_date': fr.due_date.isoformat(),
                'receipts': [{'id': p.id, 'number': p.receipt_number} for p in fr.payments.all()]
            })
        return JsonResponse(records, safe=False)



@require_tenant_type(['school'])
def student_payments_api(request, schema_name, student_id):
    """API: Return JSON list of payments for a student."""
    from django.http import JsonResponse
    from .models import Student
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        try:
            student = Student.objects.get(id=student_id)
        except Student.DoesNotExist:
            return JsonResponse({'error': 'Student not found'}, status=404)
        payments = []
        for p in student.payments.all().order_by('-payment_date'):
            payments.append({
                'id': p.id,
                'receipt_number': p.receipt_number,
                'amount': float(p.amount),
                'date': p.payment_date.isoformat(),
                'mode': p.get_payment_mode_display(),
                'remarks': p.remarks or '',
                'url': f'/portal/{schema_name}/fee/receipt/{p.id}/'
            })
        return JsonResponse(payments, safe=False)



@require_tenant_type(['school'])
def student_current_fee_status_api(request, schema_name, student_id):
    print(f"[DEBUG] student_current_fee_status_api called for student {student_id}, schema {schema_name}")
    """API: Get current month's fee record status for a student."""
    from django.http import JsonResponse
    from django.utils import timezone
    from .models import Student, FeeRecord
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        try:
            student = Student.objects.get(id=student_id)
        except Student.DoesNotExist:
            return JsonResponse({'error': 'Student not found'}, status=404)
        today = timezone.localdate()
        month = today.month
        year = today.year
        try:
            record = FeeRecord.objects.get(student=student, month=month, year=year)
            data = {
                'exists': True,
                'amount': float(record.total_amount),
                'paid_amount': float(record.paid_amount),
                'status': record.get_status_display(),
                'due_date': record.due_date.isoformat(),
                'can_edit': record.paid_amount == 0   # allow edit only if unpaid
            }
        except FeeRecord.DoesNotExist:
            # Return default fee info
            default_fee = float(student.custom_fee) if student.custom_fee > 0 else 0
            if default_fee == 0:
                from .models import FeeStructure
                fee_struct = FeeStructure.objects.filter(grade=student.grade).first()
                if fee_struct:
                    default_fee = float(fee_struct.monthly_fee)
            data = {
                'exists': False,
                'default_fee': default_fee,
                'grade': student.grade
            }
        return JsonResponse(data)



def gym_generate_subscription(request, schema_name, customer_id):
    """Generate a new subscription for a gym customer (multi-month)."""
    from django.http import JsonResponse
    from django.utils import timezone
    from decimal import Decimal
    from .models import GymCustomer, GymSubscription, GymSettings
    from datetime import date, timedelta
    from calendar import monthrange
    import json
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        try:
            customer = GymCustomer.objects.get(id=customer_id)
        except GymCustomer.DoesNotExist:
            return JsonResponse({'error': 'Customer not found'}, status=404)

        if request.method != 'POST':
            return JsonResponse({'error': 'Only POST allowed'}, status=405)

        try:
            data = json.loads(request.body)
            months = int(data.get('months', 1))
            monthly_fee = Decimal(str(data.get('fee', customer.monthly_fee)))
        except (ValueError, TypeError, json.JSONDecodeError):
            return JsonResponse({'error': 'Invalid data. Provide months and fee.'}, status=400)

        if months < 1 or months > 12:
            return JsonResponse({'error': 'Months must be between 1 and 12'}, status=400)

        today = date.today()
        settings = GymSettings.objects.first()
        if not settings:
            settings = GymSettings.objects.create()
        due_offset = settings.due_date_offset

        created = []

        for i in range(months):
            target_month = today.month + i
            target_year = today.year
            while target_month > 12:
                target_month -= 12
                target_year += 1

            # ✅ Compute due_date for this target month BEFORE checking existence
            due_day = customer.membership_start.day if customer.membership_start else 1
            max_day = monthrange(target_year, target_month)[1]
            due_day = min(due_day, max_day)
            due_date = date(target_year, target_month, due_day) + timedelta(days=due_offset)

            existing = GymSubscription.objects.filter(customer=customer, month=target_month, year=target_year).first()
            if existing:
                if existing.is_cancelled:
                    # Reactivate cancelled subscription with new parameters
                    existing.amount = monthly_fee
                    existing.paid_amount = Decimal('0')
                    existing.due_date = due_date          # ✅ now due_date is defined
                    existing.status = 'pending'
                    existing.is_cancelled = False
                    existing.cancelled_on = None
                    existing.save()
                    created.append(existing)
                else:
                    # Already have a valid subscription for this month
                    continue
            else:
                # No subscription → create new one
                sub = GymSubscription.objects.create(
                    customer=customer,
                    month=target_month,
                    year=target_year,
                    amount=monthly_fee,
                    due_date=due_date,
                    status='pending'
                )
                created.append(sub)

        if created:
            return JsonResponse({'message': f'Generated {len(created)} subscription(s).'})
        else:
            return JsonResponse({'message': 'No new subscriptions created (already exist).'})



@require_tenant_type(['gym'])
def gym_cancel_subscription(request, schema_name, subscription_id):
    """Cancel a gym subscription with partial refund calculation."""
    from django.http import JsonResponse
    from django.utils import timezone
    from decimal import Decimal
    from .models import GymSubscription
    from datetime import date
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        try:
            sub = GymSubscription.objects.get(id=subscription_id)
        except GymSubscription.DoesNotExist:
            return JsonResponse({'error': 'Subscription not found'}, status=404)

        if sub.is_cancelled:
            return JsonResponse({'error': 'Already cancelled'}, status=400)

        if request.method != 'POST':
            return JsonResponse({'error': 'Only POST allowed'}, status=405)

        # Calculate partial refund (unused days)
        today = date.today()
        month_start = date(sub.year, sub.month, 1)
        if sub.month == 12:
            next_month = date(sub.year+1, 1, 1)
        else:
            next_month = date(sub.year, sub.month+1, 1)
        days_in_month = (next_month - month_start).days
        days_used = max(0, (today - month_start).days)
        if days_used >= days_in_month:
            # Already fully into month, no refund
            refund = Decimal('0')
        else:
            daily_rate = sub.amount / Decimal(days_in_month)
            remaining_days = days_in_month - days_used
            refund = daily_rate * Decimal(remaining_days)
        sub.status = 'cancelled'
        sub.is_cancelled = True
        sub.cancelled_on = today
        sub.save()

        # Optionally create a refund adjustment (negative payment) – not implemented here
        return JsonResponse({
            'message': f'Subscription cancelled. Refund amount (estimated): ₹{refund:.2f}',
            'refund': float(refund)
        })



@require_tenant_type(['gym'])
def gym_update_subscription(request, schema_name, subscription_id):
    """Update amount of an existing unpaid subscription."""
    from django.http import JsonResponse
    from django_tenants.utils import schema_context
    from .models import GymSubscription
    import json
    from decimal import Decimal

    with schema_context(schema_name):
        try:
            sub = GymSubscription.objects.get(id=subscription_id)
        except GymSubscription.DoesNotExist:
            return JsonResponse({'error': 'Subscription not found'}, status=404)

        if sub.paid_amount > 0:
            return JsonResponse({'error': 'Cannot edit a subscription that already has payments'}, status=400)
        if sub.is_cancelled:
            return JsonResponse({'error': 'Cancelled subscriptions cannot be edited'}, status=400)

        if request.method != 'POST':
            return JsonResponse({'error': 'Only POST allowed'}, status=405)

        try:
            data = json.loads(request.body)
            new_amount = Decimal(str(data.get('amount')))
            if new_amount <= 0:
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            return JsonResponse({'error': 'Invalid amount'}, status=400)

        sub.amount = new_amount
        sub.save()
        return JsonResponse({'message': f'Subscription amount updated to ₹{new_amount}'})





def gym_edit_attendance(request, schema_name, attendance_id):
    """Edit an existing attendance record (within 7 hours of check-in)."""
    from django.http import JsonResponse
    from django.utils import timezone
    from .models import GymAttendance
    from django_tenants.utils import schema_context
    import json
    from datetime import datetime

    with schema_context(schema_name):
        try:
            att = GymAttendance.objects.get(id=attendance_id)
        except GymAttendance.DoesNotExist:
            return JsonResponse({'error': 'Attendance not found'}, status=404)

        if request.method == 'GET':
            # Return current data for editing
            return JsonResponse({
                'id': att.id,
                'check_in': att.check_in.isoformat() if att.check_in else '',
                'check_out': att.check_out.isoformat() if att.check_out else '',
                'notes': att.notes or ''
            })

        if request.method == 'POST':
            # Check edit window
            if not att.is_editable():
                return JsonResponse({'error': 'Edit window expired (7 hours after check-in).'}, status=403)

            try:
                data = request.POST
                check_in_str = data.get('check_in')
                check_out_str = data.get('check_out')
                notes = data.get('notes', '')

                if check_in_str:
                    att.check_in = datetime.fromisoformat(check_in_str)
                if check_out_str:
                    att.check_out = datetime.fromisoformat(check_out_str)
                att.notes = notes
                # Recalculate duration
                if att.check_out:
                    att.duration_minutes = int((att.check_out - att.check_in).total_seconds() / 60)
                else:
                    att.duration_minutes = None
                att.save()
                return JsonResponse({'message': 'Attendance updated successfully.'})
            except Exception as e:
                return JsonResponse({'error': str(e)}, status=400)

        return JsonResponse({'error': 'Method not allowed'}, status=405)



# ==================== GYM VIEWS (Added by patcher) ====================



@require_tenant_type(['gym'])
def gym_dashboard(request, schema_name):
    """Gym dashboard view."""
    from django.shortcuts import render
    from django.utils import timezone
    from django.db.models import Sum, Count
    from .models import GymCustomer, GymPayment, GymAttendance, GymSubscription
    from datetime import date, timedelta
    from decimal import Decimal
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        today = timezone.localdate()
        # Today's check-ins
        today_checkins = GymAttendance.objects.filter(date=today).count()
        # Active customers count
        active_customers = GymCustomer.objects.filter(status='active').count()
        # Revenue this month
        first_day_month = today.replace(day=1)
        month_revenue = GymPayment.objects.filter(payment_date__gte=first_day_month).aggregate(Sum('amount'))['amount__sum'] or Decimal(0)
        # Expiring soon (next 7 days)
        expiring_soon = GymCustomer.objects.filter(
            status='active',
            membership_end__gte=today,
            membership_end__lte=today + timedelta(days=7)
        ).count()
        # Recent payments
        recent_payments = list(GymPayment.objects.select_related('customer').order_by('-payment_date')[:5])
        # Monthly revenue trend (last 6 months)
        months_labels = []
        months_amounts = []
        for i in range(5, -1, -1):
            m = today.month - i
            y = today.year
            if m <= 0:
                m += 12
                y -= 1
            total = GymPayment.objects.filter(payment_date__year=y, payment_date__month=m).aggregate(Sum('amount'))['amount__sum'] or 0
            months_labels.append(f"{m}/{y}")
            months_amounts.append(float(total))

        context = {
            'tenant': get_tenant(request, schema_name),
            'today_checkins': today_checkins,
            'active_customers': active_customers,
            'month_revenue': month_revenue,
            'expiring_soon': expiring_soon,
            'recent_payments': recent_payments,
            'months_labels': months_labels,
            'months_amounts': months_amounts,
            'today': today,
            'logo_url': get_tenant(request, schema_name).school_logo.url if get_tenant(request, schema_name).school_logo else None,
        }
        return render(request, 'tenant/gym_dashboard.html', context)




@require_tenant_type(['gym'])
def gym_customer_list(request, schema_name):
    """List all gym customers."""
    from django.shortcuts import render
    from django.db.models import Sum
    from .models import GymCustomer, GymSubscription
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        q = request.GET.get('q', '')
        status_filter = request.GET.get('status', '')
        customers = GymCustomer.objects.all()
        if q:
            customers = customers.filter(name__icontains=q) | customers.filter(phone__icontains=q)
        if status_filter:
            customers = customers.filter(status=status_filter)

        # Annotate pending amount
        for c in customers:
            pending = sum(s.remaining for s in c.subscriptions.filter(status__in=['pending', 'partial', 'overdue']))
            c.pending_amount = pending

        status_choices = GymCustomer.STATUS_CHOICES
        context = {
            'tenant': get_tenant(request, schema_name),
            'customers': customers,
            'status_choices': status_choices,
            'logo_url': get_tenant(request, schema_name).school_logo.url if get_tenant(request, schema_name).school_logo else None,
        }
        return render(request, 'tenant/gym_customer_list.html', context)




@require_tenant_type(['gym'])
def gym_customer_add(request, schema_name):
    """Add a new gym customer."""
    from django.shortcuts import render, redirect
    from .forms import GymCustomerForm
    from django.contrib import messages
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        if request.method == 'POST':
            form = GymCustomerForm(request.POST, request.FILES)
            if form.is_valid():
                customer = form.save()
                messages.success(request, f"Customer {customer.name} added successfully.")
                return redirect('gym_customer_profile', schema_name=schema_name, customer_id=customer.id)
        else:
            form = GymCustomerForm()
        context = {
            'tenant': get_tenant(request, schema_name),
            'form': form,
            'logo_url': get_tenant(request, schema_name).school_logo.url if get_tenant(request, schema_name).school_logo else None,
        }
        return render(request, 'tenant/gym_customer_form.html', context)




@require_tenant_type(['gym'])
def gym_customer_edit(request, schema_name, customer_id):
    """Edit an existing gym customer."""
    from django.shortcuts import render, redirect, get_object_or_404
    from .forms import GymCustomerForm
    from .models import GymCustomer
    from django.contrib import messages
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        customer = get_object_or_404(GymCustomer, id=customer_id)
        if request.method == 'POST':
            form = GymCustomerForm(request.POST, request.FILES, instance=customer)
            if form.is_valid():
                form.save()
                messages.success(request, f"Customer {customer.name} updated.")
                return redirect('gym_customer_profile', schema_name=schema_name, customer_id=customer.id)
        else:
            form = GymCustomerForm(instance=customer)
        context = {
            'tenant': get_tenant(request, schema_name),
            'form': form,
            'customer': customer,
            'logo_url': get_tenant(request, schema_name).school_logo.url if get_tenant(request, schema_name).school_logo else None,
        }
        return render(request, 'tenant/gym_customer_form.html', context)




@require_tenant_type(['gym'])
def gym_customer_profile(request, schema_name, customer_id):
    """Display customer profile with subscriptions and payments."""
    from django.shortcuts import render, get_object_or_404
    from .models import GymCustomer, GymSubscription, GymPayment, GymAttendance
    from django_tenants.utils import schema_context
    from django.utils import timezone

    with schema_context(schema_name):
        customer = get_object_or_404(GymCustomer, id=customer_id)
        subscriptions = customer.subscriptions.all().order_by('-year', '-month')
        payments = customer.payments.all().order_by('-payment_date')
        attendances = customer.attendances.all().order_by('-date')
        for a in attendances:
            a.can_edit = a.is_editable()

        total_fee = subscriptions.aggregate(Sum('amount'))['amount__sum'] or 0
        total_paid = payments.aggregate(Sum('amount'))['amount__sum'] or 0
        pending_total = total_fee - total_paid

        context = {
            'tenant': get_tenant(request, schema_name),
            'customer': customer,
            'subscriptions': subscriptions,
            'payments': payments,
            'attendances': attendances,
            'total_fee': total_fee,
            'total_paid': total_paid,
            'pending_total': pending_total,
            'logo_url': get_tenant(request, schema_name).school_logo.url if get_tenant(request, schema_name).school_logo else None,
        }
        return render(request, 'tenant/gym_customer_profile.html', context)




@require_tenant_type(['gym'])
def gym_payment(request, schema_name, customer_id=None):
    """Collect payment for gym customer."""
    from django.shortcuts import render, redirect, get_object_or_404
    from django.contrib import messages
    from decimal import Decimal
    from .models import GymCustomer, GymSubscription, GymPayment
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        # Get all customers with pending fees for the list
        customers_with_pending = []
        for c in GymCustomer.objects.filter(status='active'):
            pending = sum(s.remaining for s in c.subscriptions.filter(status__in=['pending', 'partial', 'overdue']))
            if pending > 0:
                c.pending_amount = pending
                customers_with_pending.append(c)
        customers_with_pending.sort(key=lambda x: x.pending_amount, reverse=True)

        selected_customer = None
        total_pending = 0
        pending_subs = []

        if customer_id:
            selected_customer = get_object_or_404(GymCustomer, id=customer_id)
            pending_subs = selected_customer.subscriptions.filter(status__in=['pending', 'partial', 'overdue']).order_by('due_date')
            total_pending = sum(s.remaining for s in pending_subs)

        if request.method == 'POST':
            cust_id = request.POST.get('customer_id')
            amount = request.POST.get('amount')
            payment_mode = request.POST.get('payment_mode')
            remarks = request.POST.get('remarks', '')
            if cust_id and amount:
                try:
                    customer = GymCustomer.objects.get(id=cust_id)
                    amount = Decimal(amount)
                    pending_subs_list = customer.subscriptions.filter(status__in=['pending', 'partial', 'overdue']).order_by('due_date')
                    total_pending_sum = sum(s.remaining for s in pending_subs_list)
                    if amount > total_pending_sum:
                        messages.error(request, f"Amount exceeds total pending (₹{total_pending_sum})")
                        return redirect('gym_payment', schema_name=schema_name, customer_id=customer.id)

                    remaining = amount
                    paid_subs = []
                    for sub in pending_subs_list:
                        if remaining <= 0:
                            break
                        due = sub.remaining_total
                        if remaining >= due:
                            sub.paid_amount = sub.total_amount
                            remaining -= due
                        else:
                            sub.paid_amount += remaining
                            remaining = 0
                        sub.save()
                        paid_subs.append(sub)

                    payment = GymPayment.objects.create(
                        customer=customer,
                        amount=amount,
                        payment_mode=payment_mode,
                        payment_type='full' if remaining == 0 else 'partial',
                        remarks=remarks,
                        created_by=request.session.get('school_admin_username', 'admin')
                    )
                    payment.subscriptions.set(paid_subs)
                    messages.success(request, f"Payment of ₹{amount} received. Receipt: {payment.receipt_number}")
                    return redirect('gym_receipt', schema_name=schema_name, receipt_id=payment.id)
                except Exception as e:
                    messages.error(request, f"Error processing payment: {str(e)}")
            else:
                messages.error(request, "Invalid payment data")
            return redirect('gym_payment', schema_name=schema_name)

        recent_payments = list(GymPayment.objects.select_related('customer').order_by('-payment_date')[:5])

        context = {
            'tenant': get_tenant(request, schema_name),
            'customers': customers_with_pending,
            'selected_customer': selected_customer,
            'total_pending': total_pending,
            'pending_subs': pending_subs,
            'recent_payments': recent_payments,
            'logo_url': get_tenant(request, schema_name).school_logo.url if get_tenant(request, schema_name).school_logo else None,
        }
        return render(request, 'tenant/gym_payment.html', context)




@require_tenant_type(['gym'])
def gym_receipt(request, schema_name, receipt_id):
    """Display gym payment receipt."""
    from django.shortcuts import render, get_object_or_404
    from .models import GymPayment
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        payment = get_object_or_404(GymPayment, id=receipt_id)
        subscriptions = list(payment.subscriptions.all())
        context = {
            'tenant': get_tenant(request, schema_name),
            'payment': payment,
            'subscriptions': subscriptions,
            'logo_url': get_tenant(request, schema_name).school_logo.url if get_tenant(request, schema_name).school_logo else None,
            'payment_type_display': payment.payment_type,   # added to fix missing method
        }
        return render(request, 'tenant/gym_receipt.html', context)


def gym_reports(request, schema_name):
    """Gym reports and analytics page."""
    from django.shortcuts import render
    from django.db.models import Sum
    from .models import GymCustomer, GymSubscription, GymPayment, GymAttendance
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        # Stats for overview tab
        total_revenue_all = GymPayment.objects.aggregate(Sum('amount'))['amount__sum'] or 0
        total_checkins_all = GymAttendance.objects.count()
        active_customers = GymCustomer.objects.filter(status='active').count()
        active_subs = GymSubscription.objects.filter(status__in=['pending', 'partial']).count()
        from datetime import date, timedelta
        today = date.today()
        expiring_soon = GymCustomer.objects.filter(
            status='active',
            membership_end__gte=today,
            membership_end__lte=today + timedelta(days=7)
        ).count()

        context = {
            'tenant': get_tenant(request, schema_name),
            'total_revenue_all': total_revenue_all,
            'total_checkins_all': total_checkins_all,
            'active_customers': active_customers,
            'active_subs': active_subs,
            'expiring_soon': expiring_soon,
            'logo_url': get_tenant(request, schema_name).school_logo.url if get_tenant(request, schema_name).school_logo else None,
        }
        return render(request, 'tenant/gym_reports.html', context)




@require_tenant_type(['gym'])
def gym_settings(request, schema_name):
    """Gym settings view."""
    from django.shortcuts import render, redirect
    from .forms import GymSettingsForm
    from .models import GymSettings
    from django.contrib import messages
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        settings_obj, created = GymSettings.objects.get_or_create(pk=1)
        if request.method == 'POST':
            form = GymSettingsForm(request.POST, instance=settings_obj)
            if form.is_valid():
                form.save()
                messages.success(request, "Gym settings updated.")
                return redirect('gym_settings', schema_name=schema_name)
        else:
            form = GymSettingsForm(instance=settings_obj)

        context = {
            'tenant': get_tenant(request, schema_name),
            'form': form,
            'logo_url': get_tenant(request, schema_name).school_logo.url if get_tenant(request, schema_name).school_logo else None,
        }
        return render(request, 'tenant/gym_settings.html', context)









@require_tenant_type(['gym'])
def gym_attendance(request, schema_name):
    """Attendance management page."""
    from django.shortcuts import render
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        context = {
            'tenant': get_tenant(request, schema_name),
            'logo_url': get_tenant(request, schema_name).school_logo.url if get_tenant(request, schema_name).school_logo else None,
        }
        return render(request, 'tenant/gym_attendance.html', context)


# ==================== GYM API VIEWS ====================





def gym_checkin_api(request):
    """API: Check in a gym customer (barcode or ID)."""
    from django.http import JsonResponse
    from django.utils import timezone
    from .models import GymCustomer, GymAttendance
    import json

    if not request.session.get('school_admin_authenticated'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    schema_name = request.session.get('school_admin_schema')
    if not schema_name:
        return JsonResponse({'error': 'No tenant schema'}, status=400)
    try:
        tenant = SchoolClient.objects.get(schema_name=schema_name)
    except SchoolClient.DoesNotExist:
        return JsonResponse({'error': 'Tenant not found'}, status=404)

    customer_id = request.POST.get('customer_id')
    if not customer_id:
        return JsonResponse({'error': 'customer_id required'}, status=400)

    with schema_context(schema_name):
        try:
            customer = GymCustomer.objects.get(id=customer_id, status='active')
        except GymCustomer.DoesNotExist:
            return JsonResponse({'error': 'Customer not found or inactive'}, status=404)

        # Check if already checked in today
        today = timezone.localdate()
        existing = GymAttendance.objects.filter(customer=customer, date=today, check_out__isnull=True).first()
        if existing:
            return JsonResponse({'error': f'{customer.name} is already checked in today at {existing.check_in.strftime("%H:%M")}'}, status=400)

        # Create attendance record
        now = timezone.now()
        attendance = GymAttendance.objects.create(
            customer=customer,
            date=today,
            check_in=now,
            notes=f"Checked in via API at {now.strftime('%H:%M')}"
        )
        return JsonResponse({
            'message': f'{customer.name} checked in successfully',
            'customer_name': customer.name,
            'attendance_id': attendance.id,
            'check_in_time': now.isoformat()
        })




@csrf_exempt
@require_http_methods(["POST"])
def gym_checkout_api(request):
    """API: Check out a gym customer."""
    from django.http import JsonResponse
    from django.utils import timezone
    from .models import GymCustomer, GymAttendance

    if not request.session.get('school_admin_authenticated'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    schema_name = request.session.get('school_admin_schema')
    if not schema_name:
        return JsonResponse({'error': 'No tenant schema'}, status=400)
    try:
        tenant = SchoolClient.objects.get(schema_name=schema_name)
    except SchoolClient.DoesNotExist:
        return JsonResponse({'error': 'Tenant not found'}, status=404)

    customer_id = request.POST.get('customer_id')
    if not customer_id:
        return JsonResponse({'error': 'customer_id required'}, status=400)

    with schema_context(schema_name):
        try:
            customer = GymCustomer.objects.get(id=customer_id)
        except GymCustomer.DoesNotExist:
            return JsonResponse({'error': 'Customer not found'}, status=404)

        today = timezone.localdate()
        attendance = GymAttendance.objects.filter(customer=customer, date=today, check_out__isnull=True).first()
        if not attendance:
            return JsonResponse({'error': f'{customer.name} is not checked in today'}, status=400)

        now = timezone.now()
        attendance.check_out = now
        duration = (now - attendance.check_in).total_seconds() / 60
        attendance.duration_minutes = int(duration)
        attendance.notes = (attendance.notes or '') + f" Checked out at {now.strftime('%H:%M')}"
        attendance.save()
        return JsonResponse({
            'message': f'{customer.name} checked out',
            'customer_name': customer.name,
            'duration_minutes': attendance.duration_minutes
        })




def gym_revenue_stats_api(request, schema_name):
    """API: Revenue statistics for gym."""
    from django.http import JsonResponse
    from django.db.models import Sum, Count
    from .models import GymPayment
    from django_tenants.utils import schema_context
    from datetime import date, timedelta
    from collections import defaultdict
    from decimal import Decimal

    start_str = request.GET.get('start')
    end_str = request.GET.get('end')
    group_by = request.GET.get('group_by', 'month')  # 'day' or 'month'

    with schema_context(schema_name):
        qs = GymPayment.objects.all()
        if start_str:
            qs = qs.filter(payment_date__gte=date.fromisoformat(start_str))
        if end_str:
            qs = qs.filter(payment_date__lte=date.fromisoformat(end_str))

        total_revenue = qs.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        transaction_count = qs.count()

        # Mode distribution
        mode_totals = defaultdict(Decimal)
        for p in qs:
            mode_totals[p.get_payment_mode_display()] += p.amount
        mode_distribution = [{'name': k, 'amount': float(v)} for k, v in mode_totals.items()]

        # Top spenders
        top_spenders = []
        customer_totals = defaultdict(Decimal)
        for p in qs.select_related('customer'):
            customer_totals[p.customer.name] += p.amount
        for name, total in sorted(customer_totals.items(), key=lambda x: x[1], reverse=True)[:5]:
            top_spenders.append({'name': name, 'total': float(total)})

        # Time series
        labels = []
        amounts = []
        if group_by == 'month':
            # Group by month-year
            monthly = {}
            for p in qs:
                key = f"{p.payment_date.year}-{p.payment_date.month:02d}"
                monthly[key] = monthly.get(key, Decimal('0')) + p.amount
            # Sort by date
            for key in sorted(monthly.keys()):
                labels.append(key)
                amounts.append(float(monthly[key]))
        else:
            # Daily
            daily = {}
            for p in qs:
                key = p.payment_date.isoformat()
                daily[key] = daily.get(key, Decimal('0')) + p.amount
            for key in sorted(daily.keys()):
                labels.append(key)
                amounts.append(float(daily[key]))

        return JsonResponse({
            'total_revenue': float(total_revenue),
            'transaction_count': transaction_count,
            'mode_distribution': mode_distribution,
            'top_spenders': top_spenders,
            'labels': labels,
            'amounts': amounts
        })




def gym_attendance_stats_api(request, schema_name):
    """API: Attendance statistics for gym."""
    from django.http import JsonResponse
    from django.db.models import Count
    from .models import GymAttendance
    from django_tenants.utils import schema_context
    from datetime import date, timedelta
    from collections import defaultdict

    start_str = request.GET.get('start')
    end_str = request.GET.get('end')

    with schema_context(schema_name):
        qs = GymAttendance.objects.all()
        if start_str:
            qs = qs.filter(date__gte=date.fromisoformat(start_str))
        if end_str:
            qs = qs.filter(date__lte=date.fromisoformat(end_str))

        total_checkins = qs.count()
        unique_customers = qs.values('customer').distinct().count()
        avg_per_day = 0
        if start_str and end_str:
            days = (date.fromisoformat(end_str) - date.fromisoformat(start_str)).days + 1
            if days > 0:
                avg_per_day = round(total_checkins / days, 1)

        # Daily counts
        daily_counts = defaultdict(int)
        for a in qs:
            daily_counts[a.date.isoformat()] += 1
        labels = sorted(daily_counts.keys())
        counts = [daily_counts[d] for d in labels]

        # Hour distribution
        hour_counts = defaultdict(int)
        for a in qs:
            hour = a.check_in.hour
            hour_counts[hour] += 1
        hour_labels = list(range(24))
        hour_counts_list = [hour_counts[h] for h in hour_labels]

        return JsonResponse({
            'total_checkins': total_checkins,
            'unique_customers': unique_customers,
            'avg_per_day': avg_per_day,
            'labels': labels,
            'counts': counts,
            'hour_labels': hour_labels,
            'hour_counts': hour_counts_list
        })




def gym_customers_list_api(request, schema_name):
    """API: List customers with optional search and status filter."""
    from django.http import JsonResponse
    from django.db.models import Sum
    from .models import GymCustomer, GymSubscription, GymPayment, GymAttendance
    from django_tenants.utils import schema_context

    search = request.GET.get('search', '')
    status = request.GET.get('status', '')

    with schema_context(schema_name):
        qs = GymCustomer.objects.all()
        if search:
            qs = qs.filter(name__icontains=search) | qs.filter(phone__icontains=search)
        if status:
            qs = qs.filter(status=status)

        customers = []
        for c in qs:
            total_paid = c.payments.aggregate(Sum('amount'))['amount__sum'] or 0
            total_billed = c.subscriptions.aggregate(Sum('amount'))['amount__sum'] or 0
            pending = total_billed - total_paid
            attendance_count = c.attendances.count()
            customers.append({
                'id': c.id,
                'name': c.name,
                'phone': c.phone,
                'status': c.status,
                'membership_end': c.membership_end.isoformat() if c.membership_end else None,
                'total_paid': float(total_paid),
                'pending': float(pending),
                'attendance_count': attendance_count
            })
        return JsonResponse(customers, safe=False)




def gym_customer_detail_api(request, schema_name, customer_id):
    """API: Detailed customer data for modal."""
    from django.http import JsonResponse
    from django.db.models import Sum
    from .models import GymCustomer, GymSubscription, GymPayment, GymAttendance
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        try:
            c = GymCustomer.objects.get(id=customer_id)
        except GymCustomer.DoesNotExist:
            return JsonResponse({'error': 'Customer not found'}, status=404)

        total_paid = c.payments.aggregate(Sum('amount'))['amount__sum'] or 0
        total_billed = c.subscriptions.aggregate(Sum('amount'))['amount__sum'] or 0
        pending = total_billed - total_paid

        payments = [{
            'receipt': p.receipt_number,
            'amount': float(p.amount),
            'date': p.payment_date.isoformat(),
            'mode': p.get_payment_mode_display()
        } for p in c.payments.order_by('-payment_date')]

        subscriptions = [{
            'month': f"{s.month}/{s.year}",
            'amount': float(s.amount),
            'paid': float(s.paid_amount),
            'status': s.get_status_display(),
            'cancelled': s.is_cancelled
        } for s in c.subscriptions.order_by('-year', '-month')]

        attendances = [{
            'date': a.date.isoformat(),
            'check_in': a.check_in.isoformat(),
            'check_out': a.check_out.isoformat() if a.check_out else None
        } for a in c.attendances.order_by('-date')]

        return JsonResponse({
            'id': c.id,
            'name': c.name,
            'phone': c.phone,
            'email': c.email,
            'status': c.status,
            'membership_start': c.membership_start.isoformat(),
            'membership_end': c.membership_end.isoformat() if c.membership_end else None,
            'monthly_fee': float(c.monthly_fee),
            'total_paid': float(total_paid),
            'pending': float(pending),
            'payments': payments,
            'subscriptions': subscriptions,
            'attendances': attendances
        })




def gym_subscription_status_api(request, schema_name):
    """API: Subscription status counts and expiring lists."""
    from django.http import JsonResponse
    from .models import GymCustomer, GymSubscription
    from django_tenants.utils import schema_context
    from datetime import date, timedelta

    with schema_context(schema_name):
        today = date.today()
        current_month = today.month
        current_year = today.year

        active_subs = GymSubscription.objects.filter(month=current_month, year=current_year, status__in=['pending', 'partial'])
        active_count = active_subs.count()
        active_subscriptions = [{'customer': s.customer.name, 'amount': float(s.amount)} for s in active_subs]

        expiring_customers = GymCustomer.objects.filter(
            status='active',
            membership_end__gte=today,
            membership_end__lte=today + timedelta(days=7)
        )
        expiring_count = expiring_customers.count()
        expiring_soon = [{'name': c.name, 'phone': c.phone, 'end_date': c.membership_end.isoformat()} for c in expiring_customers]

        expired_customers = GymCustomer.objects.filter(status='expired') | GymCustomer.objects.filter(membership_end__lt=today, status='active')
        expired_count = expired_customers.count()
        expired_list = [{'name': c.name, 'phone': c.phone, 'end_date': c.membership_end.isoformat() if c.membership_end else 'Unknown'} for c in expired_customers]

        return JsonResponse({
            'active_count': active_count,
            'active_subscriptions': active_subscriptions,
            'expiring_count': expiring_count,
            'expiring_soon': expiring_soon,
            'expired_count': expired_count,
            'expired_customers': expired_list
        })




def gym_attendance_data_api(request, schema_name):
    """API: Get active check-ins and today's history for attendance page."""
    from django.http import JsonResponse
    from .models import GymAttendance, GymCustomer
    from django.utils import timezone
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        today = timezone.localdate()
        # Active check-ins (not checked out yet)
        active = GymAttendance.objects.filter(date=today, check_out__isnull=True).select_related('customer')
        active_data = []
        for a in active:
            active_data.append({
                'id': a.id,
                'customer_id': a.customer.id,
                'customer_name': a.customer.name,
                'customer_phone': a.customer.phone,
                'check_in_time': a.check_in.strftime('%H:%M:%S'),
                'check_in_raw': a.check_in.isoformat()
            })
        # Today's history (checked out)
        history = GymAttendance.objects.filter(date=today, check_out__isnull=False).select_related('customer')
        history_data = []
        for a in history:
            duration = None
            if a.duration_minutes:
                duration = f"{a.duration_minutes // 60}h {a.duration_minutes % 60}m"
            history_data.append({
                'id': a.id,
                'customer_name': a.customer.name,
                'customer_phone': a.customer.phone,
                'check_in_time': a.check_in.strftime('%H:%M:%S'),
                'check_out_time': a.check_out.strftime('%H:%M:%S') if a.check_out else None,
                'duration': duration,
                'notes': a.notes
            })
        # Stats
        today_stats = {
            'total': GymAttendance.objects.filter(date=today).count(),
            'active': active.count(),
            'unique': GymAttendance.objects.filter(date=today).values('customer').distinct().count()
        }
        return JsonResponse({
            'active': active_data,
            'history': history_data,
            'today_stats': today_stats
        })




def gym_eligible_customers_api(request, schema_name):
    """API: Return customers eligible for check-in (active, not already checked in today)."""
    from django.http import JsonResponse
    from .models import GymCustomer, GymAttendance
    from django.utils import timezone
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        today = timezone.localdate()
        checked_in_ids = set(GymAttendance.objects.filter(date=today).values_list('customer_id', flat=True))
        eligible = GymCustomer.objects.filter(status='active').exclude(id__in=checked_in_ids)
        data = [{'id': c.id, 'name': c.name, 'phone': c.phone} for c in eligible]
        return JsonResponse(data, safe=False)




def gym_search_customer_api(request, schema_name):
    """API: Search customers by query (phone, name, barcode)."""
    from django.http import JsonResponse
    from django.db.models import Q
    from .models import GymCustomer
    from django_tenants.utils import schema_context

    q = request.GET.get('q', '')
    with schema_context(schema_name):
        customers = GymCustomer.objects.filter(
            Q(name__icontains=q) | Q(phone__icontains=q) | Q(barcode__icontains=q)
        )[:10]
        data = [{'id': c.id, 'name': c.name, 'phone': c.phone} for c in customers]
        return JsonResponse(data, safe=False)




def gym_export_attendance_api(request, schema_name):
    """API: Export attendance for a given date as CSV."""
    from django.http import HttpResponse
    from .models import GymAttendance
    from django.utils import timezone
    from django_tenants.utils import schema_context
    import csv

    date_str = request.GET.get('date', 'today')
    if date_str == 'today':
        target_date = timezone.localdate()
    else:
        target_date = date.fromisoformat(date_str)

    with schema_context(schema_name):
        attendances = GymAttendance.objects.filter(date=target_date).select_related('customer')
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="attendance_{target_date}.csv"'
        writer = csv.writer(response)
        writer.writerow(['Customer Name', 'Phone', 'Check In', 'Check Out', 'Duration (min)', 'Notes'])
        for a in attendances:
            duration = a.duration_minutes if a.duration_minutes else ''
            writer.writerow([
                a.customer.name,
                a.customer.phone,
                a.check_in.strftime('%Y-%m-%d %H:%M:%S'),
                a.check_out.strftime('%Y-%m-%d %H:%M:%S') if a.check_out else '',
                duration,
                a.notes or ''
            ])
        return response






def _extract_item_sales_from_remarks(remarks):
    """Extract item sale chunks from payment remarks for analytics and detail pages."""
    import re
    from decimal import Decimal

    text = remarks or ''
    marker_match = re.search(r'items sold\s*:\s*(.*)', text, flags=re.IGNORECASE)
    if not marker_match:
        marker_match = re.search(r'items sold\s+(.*)', text, flags=re.IGNORECASE)

    candidate_text = marker_match.group(1) if marker_match else text
    pattern = re.compile(
        r'(?P<name>.+?)\s*x\s*(?P<qty>\d+)\s*@\s*₹\s*(?P<price>\d+(?:\.\d+)?)\s*=\s*₹\s*(?P<total>\d+(?:\.\d+)?)',
        flags=re.IGNORECASE,
    )

    items = []
    for chunk in re.split(r';\s*', candidate_text):
        chunk = chunk.strip().strip('.').strip()
        if not chunk:
            continue
        match = pattern.search(chunk)
        if not match:
            continue
        items.append({
            'name': match.group('name').strip(),
            'quantity': int(match.group('qty')),
            'unit_price': Decimal(match.group('price')),
            'line_total': Decimal(match.group('total')),
            'raw': chunk,
        })
    return items


# ==================== STOCK MANAGEMENT VIEWS ====================



@require_tenant_type(['school'])
def stock_management(request, schema_name, force_mobile=False):
    """Main stock management page: list categories and products (RAW SQL)."""
    from django.shortcuts import render
    from django.db import connection
    from .models import ProductCategory, Product
    from django_tenants.utils import schema_context

    tenant = get_tenant(request, schema_name)
    with schema_context(schema_name):
        # RAW SQL for categories
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, name, description FROM axis_saas_productcategory ORDER BY name")
            raw_cats = cursor.fetchall()

        # RAW SQL for products
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT p.id, p.name, p.sku, p.selling_price, p.quantity, p.notes,
                       c.id as category_id, c.name as category_name
                FROM axis_saas_product p
                JOIN axis_saas_productcategory c ON p.category_id = c.id
                ORDER BY c.name, p.name
            """)
            raw_products = cursor.fetchall()

        products_qs = Product.objects.select_related('category').all().order_by('category__name', 'name')
        total_products = products_qs.count()
        total_categories = ProductCategory.objects.count()
        total_stock_value = sum((p.selling_price * p.quantity) for p in products_qs)
        low_stock_count = products_qs.filter(quantity__lt=10).count()

        item_sales = []
        total_units_sold = 0
        total_sales_value = Decimal('0.00')
        
        product_sales = defaultdict(lambda: {'units': 0, 'value': Decimal('0.00'), 'last_sale': None, 'id': None})
        # Cache product ids by name
        product_id_cache = {}
        for payment in PaymentTransaction.objects.filter(remarks__icontains='items sold').select_related('student').order_by('-payment_date')[:100]:
            for item in _extract_item_sales_from_remarks(payment.remarks):
                total_units_sold += item['quantity']
                total_sales_value += item['line_total']
                item_sales.append({
                    'payment': payment,
                    'item': item,
                    'student': payment.student,
                })
                name = item['name'].strip().lower()
                entry = product_sales[name]
                entry['units'] += item['quantity']
                entry['value'] += item['line_total']
                if entry['last_sale'] is None or payment.payment_date > entry['last_sale']:
                    entry['last_sale'] = payment.payment_date
                # Get product id if not already cached
                if entry['id'] is None and name not in product_id_cache:
                    # Try to find the product by exact name (case insensitive)
                    prod = Product.objects.filter(name__iexact=name).first()
                    if prod:
                        product_id_cache[name] = prod.id
                    else:
                        product_id_cache[name] = None
                if entry['id'] is None and name in product_id_cache:
                    entry['id'] = product_id_cache[name]

        top_items = []
        for name, values in product_sales.items():
            top_items.append({
                'name': name.title(),
                'units': values['units'],
                'value': values['value'],
                'last_sale': values['last_sale'],
                'id': values['id'],   # could be None
            })
    
        

        context = {
            'tenant': tenant,
            'raw_cats': raw_cats,
            'raw_products': raw_products,
            'analytics': {
                'total_products': total_products,
                'total_categories': total_categories,
                'total_stock_value': total_stock_value,
                'low_stock_count': low_stock_count,
                'total_units_sold': total_units_sold,
                'total_sales_value': total_sales_value,
            },
            'recent_sales': item_sales[:10],
            'top_items': top_items,
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        }
        template = 'mobile/stock_management.html' if (is_mobile_user_agent(request) or force_mobile) else 'tenant/stock_management.html'
    return render(request, template, context)



@require_tenant_type(['school'])
@require_school_feature('stock_management')
def product_detail(request, schema_name, product_id, force_mobile=False):
    """Detailed product analytics page with sales history and recent receipts."""
    tenant = get_tenant(request, schema_name)
    with schema_context(schema_name):
        product = get_object_or_404(Product.objects.select_related('category'), id=product_id)
        sales_events = []
        total_units_sold = 0
        total_sales_value = Decimal('0.00')

        last_sale_date = None
        buyer_info = {}  # id -> name
        for payment in PaymentTransaction.objects.filter(remarks__icontains='items sold').select_related('student').order_by('-payment_date'):
            for item in _extract_item_sales_from_remarks(payment.remarks):
                if item['name'].strip().lower() != product.name.strip().lower():
                    continue
                total_units_sold += item['quantity']
                total_sales_value += item['line_total']
                sales_events.append({
                    'payment': payment,
                    'item': item,
                    'student': payment.student,
                })
                if payment.student:
                    buyer_info[payment.student.id] = payment.student.name
                if last_sale_date is None or payment.payment_date > last_sale_date:
                    last_sale_date = payment.payment_date

        context = {
            'tenant': tenant,
            'product': product,
            'sales_events': sales_events[:50],
            'analytics': {
                'total_units_sold': total_units_sold,
                'total_sales_value': total_sales_value,
                'stock_value': product.selling_price * product.quantity,
                'low_stock': product.quantity < 10,
                'last_sale_date': last_sale_date,
                'average_sale_value': (total_sales_value / total_units_sold) if total_units_sold else Decimal('0.00'),
            },
            'recent_buyers': [{'id': sid, 'name': name} for sid, name in buyer_info.items()][:8],
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        }
        template = 'mobile/product_detail.html' if (is_mobile_user_agent(request) or force_mobile) else 'tenant/product_detail.html'
    return render(request, template, context)




@require_tenant_type(['school'])
def mobile_stock_management(request, schema_name):
    """Mobile-only stock management view."""
    return stock_management(request, schema_name, force_mobile=True)



@require_tenant_type(['school'])
def mobile_product_detail(request, schema_name, product_id):
    """Mobile-only product detail view."""
    return product_detail(request, schema_name, product_id, force_mobile=True)


def add_category(request, schema_name):
    """Add or edit a product category."""
    from django.shortcuts import redirect, get_object_or_404
    from django.contrib import messages
    from .models import ProductCategory
    from django_tenants.utils import schema_context

    if request.method == 'POST':
        cat_id = request.POST.get('category_id')
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        if not name:
            messages.error(request, "Category name is required")
            if is_mobile_user_agent(request):

                return redirect('mobile_stock_management', schema_name=schema_name)

            if is_mobile_user_agent(request) or request.POST.get('mobile_redirect') == '1':

                return redirect('mobile_stock_management', schema_name=schema_name)

            return redirect('stock_management', schema_name=schema_name)
        with schema_context(schema_name):
            if cat_id:
                category = get_object_or_404(ProductCategory, id=cat_id)
                category.name = name
                category.description = description
                category.save()
                messages.success(request, f"Category '{name}' updated.")
            else:
                if ProductCategory.objects.filter(name__iexact=name).exists():
                    messages.error(request, "Category with this name already exists.")
                else:
                    ProductCategory.objects.create(name=name, description=description)
                    messages.success(request, f"Category '{name}' added.")
    if is_mobile_user_agent(request) or request.POST.get('mobile_redirect') == '1':
        return redirect('mobile_stock_management', schema_name=schema_name)
    return redirect('stock_management', schema_name=schema_name)


@require_tenant_type(['school'])
@require_school_feature('stock_management')
def delete_category(request, schema_name, category_id):
    """Delete a category (only if no products linked)."""
    from django.shortcuts import get_object_or_404, redirect
    from django.contrib import messages
    from .models import ProductCategory
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        category = get_object_or_404(ProductCategory, id=category_id)
        if category.products.exists():
            messages.error(request, f"Cannot delete '{category.name}' because it has products. Remove products first.")
        else:
            category.delete()
            messages.success(request, f"Category '{category.name}' deleted.")
    if is_mobile_user_agent(request):

        return redirect('mobile_stock_management', schema_name=schema_name)

    if is_mobile_user_agent(request) or request.POST.get('mobile_redirect') == '1':

        return redirect('mobile_stock_management', schema_name=schema_name)

    return redirect('stock_management', schema_name=schema_name)


@require_tenant_type(['school'])
@require_school_feature('stock_management')
def add_product(request, schema_name):
    """Add or edit a product."""
    from django.shortcuts import redirect, get_object_or_404
    from django.contrib import messages
    from decimal import Decimal
    from .models import ProductCategory, Product
    from django_tenants.utils import schema_context

    if request.method == 'POST':
        product_id = request.POST.get('product_id')
        category_id = request.POST.get('category')
        name = request.POST.get('name', '').strip()
        selling_price = request.POST.get('selling_price')
        quantity = request.POST.get('quantity')
        notes = request.POST.get('notes', '')

        if not name or not category_id or not selling_price:
            messages.error(request, "Category, Name, and Selling Price are required.")
            if is_mobile_user_agent(request):

                return redirect('mobile_stock_management', schema_name=schema_name)

            if is_mobile_user_agent(request) or request.POST.get('mobile_redirect') == '1':

                return redirect('mobile_stock_management', schema_name=schema_name)

            return redirect('stock_management', schema_name=schema_name)
        try:
            price = Decimal(selling_price)
            qty = int(quantity) if quantity else 0
        except:
            messages.error(request, "Invalid price or quantity.")
            if is_mobile_user_agent(request) or request.POST.get('mobile_redirect') == '1':
                return redirect('mobile_stock_management', schema_name=schema_name)
            return redirect('stock_management', schema_name=schema_name)
        with schema_context(schema_name):
            category = get_object_or_404(ProductCategory, id=category_id)
            if product_id:
                product = get_object_or_404(Product, id=product_id)
                product.category = category
                product.name = name
                product.selling_price = price
                product.quantity = qty
                product.notes = notes
                product.save()
                messages.success(request, f"Product '{name}' updated.")
            else:
                product = Product.objects.create(
                    category=category,
                    name=name,
                    selling_price=price,
                    quantity=qty,
                    notes=notes
                )
                messages.success(request, f"Product '{name}' added. SKU: {product.sku}")
    if is_mobile_user_agent(request) or request.POST.get('mobile_redirect') == '1':
        return redirect('mobile_stock_management', schema_name=schema_name)
    return redirect('stock_management', schema_name=schema_name)


@require_tenant_type(['school'])
@require_school_feature('stock_management')
def delete_product(request, schema_name, product_id):
    """Delete a product."""
    from django.shortcuts import get_object_or_404, redirect
    from django.contrib import messages
    from .models import Product
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        product = get_object_or_404(Product, id=product_id)
        product.delete()
        messages.success(request, f"Product '{product.name}' deleted.")
    if is_mobile_user_agent(request):

        return redirect('mobile_stock_management', schema_name=schema_name)

    if is_mobile_user_agent(request) or request.POST.get('mobile_redirect') == '1':

        return redirect('mobile_stock_management', schema_name=schema_name)

    return redirect('stock_management', schema_name=schema_name)
# Ensure all necessary imports are present at the top of views.py (this script will not modify existing imports,
# but the functions contain their own imports; however, we need to make sure the decorator @require_tenant_type
# and get_tenant are available. They are already defined at the top of views.py.
# Also ensure that SchoolClient is imported if needed inside functions. We'll import inside each function.


# ==================== SELL SEPARATELY (standalone student search) ====================


@require_tenant_type(['school'])
def sell_separately(request, schema_name, mobile=False):
    """Page to search for a student and then redirect to fee collection for that student."""
    tenant = get_tenant(request, schema_name)
    search_query = request.GET.get('search', '').strip()
    grade_filter = request.GET.get('grade', '')
    section_filter = request.GET.get('section', '')
    search_results = []

    with schema_context(schema_name):
        # Start with all students
        students = Student.objects.all()

        # Apply grade filter if provided
        if grade_filter:
            students = students.filter(grade=grade_filter)

        # Apply section filter if provided
        if section_filter:
            students = students.filter(section=section_filter)

        # Apply search (partial matching) if query provided
        if search_query:
            students = students.filter(
                Q(name__icontains=search_query) |
                Q(roll_number__icontains=search_query) |
                Q(father_name__icontains=search_query) |
                Q(father_cnic__icontains=search_query) |
                Q(parent_mobile__icontains=search_query)
            )

        # Only fetch results if either search or any filter is present
        if search_query or grade_filter or section_filter:
            search_results = list(students.order_by('name')[:20])
        else:
            search_results = []

        # Get distinct grades and sections for filter dropdowns
        grades = list(Student.objects.values_list('grade', flat=True).distinct().order_by('grade'))
        sections = list(Student.objects.values_list('section', flat=True).distinct().order_by('section'))

    context = {
        'tenant': tenant,
        'search_query': search_query,
        'grade_filter': grade_filter,
        'section_filter': section_filter,
        'search_results': search_results,
        'grades': grades,
        'sections': sections,
        'logo_url': tenant.school_logo.url if tenant.school_logo else None,
    }
    template = 'mobile/sell_separately.html' if mobile else 'tenant/sell_separately.html'
    return render(request, template, context)



@require_tenant_type(['school'])
def mobile_sell_separately(request, schema_name):
    """Mobile version of sell separately page."""
    return sell_separately(request, schema_name, mobile=True)



# ------------------- Mobile Defaulters -------------------


@require_tenant_type(['school'])
@require_school_feature('defaulters')
def mobile_defaulters(request, schema_name):
    """Mobile version of defaulters page."""
    return defaulters(request, schema_name, force_mobile=True)


# ------------------- Mobile Reports -------------------


@require_tenant_type(['school'])
@require_school_feature('reports')
def mobile_reports(request, schema_name):
    """Mobile version of reports page."""
    return reports(request, schema_name, force_mobile=True)


# ------------------- Mobile Fee Settings -------------------


@require_tenant_type(['school'])
@require_school_feature('fee_settings')
def mobile_fee_settings(request, schema_name):
    """Mobile version of fee settings page."""
    return fee_settings(request, schema_name, force_mobile=True)




def mobile_settings(request, schema_name):
    tenant = get_tenant(request, schema_name)
    if request.method == 'POST':
        school_name = request.POST.get('school_name', '').strip()
        if school_name:
            tenant.name = school_name
        admin_username = request.POST.get('admin_username', '').strip()
        admin_password = request.POST.get('admin_password', '')
        admin_password_confirm = request.POST.get('admin_password_confirm', '')
        if admin_username:
            tenant.admin_username = admin_username
        if admin_password:
            if admin_password == admin_password_confirm:
                tenant.admin_password = admin_password
            else:
                messages.error(request, "Passwords do not match.")
                return redirect('settings', schema_name=schema_name)
        if request.FILES.get('school_logo'):
            tenant.school_logo = request.FILES['school_logo']
        tenant.save()
        messages.success(request, "Settings updated successfully.")
        return redirect('settings', schema_name=schema_name)
    context = {'tenant': tenant, 'logo_url': tenant.school_logo.url if tenant.school_logo else None}
    return render(request, 'mobile/settings.html', context)


# ------------------- VOUCHER FEATURE VIEWS -------------------


def voucher_status_api(request, schema_name, student_id):
    """API: Get current month fee status, default fee, charges, pending totals."""
    from django.http import JsonResponse
    from django.utils import timezone
    from decimal import Decimal
    from .models import Student, FeeRecord, FeeStructure, SchoolFeeSettings
    from django_tenants.utils import schema_context

    with schema_context(schema_name):
        try:
            student = Student.objects.get(id=student_id)
        except Student.DoesNotExist:
            return JsonResponse({'error': 'Student not found'}, status=404)

        today = timezone.localdate()
        month, year = today.month, today.year

        fee_record = None
        try:
            fee_record = FeeRecord.objects.get(student=student, month=month, year=year)
        except FeeRecord.DoesNotExist:
            pass

        default_fee = Decimal('0')
        fee_struct = FeeStructure.objects.filter(grade=student.grade).first()
        if fee_struct:
            default_fee = fee_struct.monthly_fee
        if student.custom_fee > 0:
            default_fee = student.custom_fee

        total_fee = student.fee_records.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        total_paid = student.payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        pending = total_fee - total_paid
        if pending < 0:
            pending = Decimal('0')

        settings, _ = SchoolFeeSettings.objects.get_or_create(pk=1)
        default_charges = settings.default_extra_charges or student.default_extra_charges or []

        response = {
            'exists': fee_record is not None,
            'fee_record': {
                'id': fee_record.id if fee_record else None,
                'amount': float(fee_record.amount) if fee_record else 0,
                'paid_amount': float(fee_record.paid_amount) if fee_record else 0,
                'extra_charges': fee_record.extra_charges or [] if fee_record else [],
                'status': fee_record.status if fee_record else None,
                'can_edit': fee_record and fee_record.paid_amount == 0,
                'due_date_offset': fee_record.due_date_offset if fee_record else settings.due_date_offset,
                'late_fee_per_day': float(fee_record.late_fee_per_day) if fee_record and fee_record.late_fee_per_day is not None else float(settings.late_fee_penalty),
            } if fee_record else None,
            'default_fee': float(default_fee),
            'default_charges': default_charges,
            'settings': {
                'due_date_offset': fee_record.due_date_offset if fee_record else settings.due_date_offset,
                'late_fee_penalty': float(fee_record.late_fee_per_day) if fee_record and fee_record.late_fee_per_day is not None else float(settings.late_fee_penalty),
            },
            'student_name': student.name,
            'student_roll': student.roll_number,
            'grade': student.grade,
            'section': student.section,
            'total_pending': float(pending),
        }
        return JsonResponse(response)




@csrf_exempt
def generate_voucher_api(request, schema_name, student_id):
    """API: Create or update fee record for current month with custom amount and charges."""
    from django.http import JsonResponse
    from django.utils import timezone
    from decimal import Decimal
    from .models import Student, FeeRecord, FeeStructure, SchoolFeeSettings
    from django_tenants.utils import schema_context
    import json

    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    custom_amount = data.get('custom_amount')
    charges = data.get('charges', [])
    save_default = data.get('save_default_charges', False)
    due_date_offset = data.get('due_date_offset')
    late_fee_penalty = data.get('late_fee_penalty')

    with schema_context(schema_name):
        try:
            student = Student.objects.get(id=student_id)
        except Student.DoesNotExist:
            return JsonResponse({'error': 'Student not found'}, status=404)

        settings, _ = SchoolFeeSettings.objects.get_or_create(pk=1)

        today = timezone.localdate()
        month, year = today.month, today.year

        if custom_amount is not None:
            try:
                amount = Decimal(str(custom_amount))
                if amount <= 0:
                    raise ValueError
            except Exception:
                return JsonResponse({'error': 'Invalid custom amount'}, status=400)
        else:
            amount = student.custom_fee if student.custom_fee > 0 else Decimal('0')
            if amount == 0:
                fee_struct = FeeStructure.objects.filter(grade=student.grade).first()
                if fee_struct:
                    amount = fee_struct.monthly_fee
            if amount <= 0:
                return JsonResponse({'error': 'No fee structure defined and no custom amount provided.'}, status=400)

        validated_charges = []
        for ch in charges:
            if isinstance(ch, dict) and 'title' in ch and 'amount' in ch:
                try:
                    ch_amount = Decimal(str(ch['amount']))
                    if ch_amount < 0:
                        continue
                    validated_charges.append({'title': str(ch['title']).strip() or 'Unnamed', 'amount': float(ch_amount)})
                except Exception:
                    pass

        effective_charges = validated_charges if validated_charges else (settings.default_extra_charges or [])

        try:
            offset_value = int(due_date_offset) if due_date_offset is not None else settings.due_date_offset
            if offset_value < 1:
                offset_value = settings.due_date_offset
        except (TypeError, ValueError):
            offset_value = settings.due_date_offset

        try:
            penalty_value = Decimal(str(late_fee_penalty)) if late_fee_penalty is not None else settings.late_fee_penalty
        except Exception:
            penalty_value = settings.late_fee_penalty

        try:
            fee_record, created = FeeRecord.objects.get_or_create(
                student=student,
                month=month,
                year=year,
                defaults={
                    'amount': amount,
                    'due_date': today + timedelta(days=offset_value),
                    'due_date_offset': offset_value,
                    'late_fee_per_day': penalty_value,
                    'status': 'pending'
                }
            )
        except Exception as exc:
            # Fallback for partially migrated databases where older rows still violate the new expectations.
            if 'due_date' in str(exc) or 'due_date_offset' in str(exc) or 'NOT NULL' in str(exc):
                fee_record = FeeRecord.objects.filter(student=student, month=month, year=year).first()
                if not fee_record:
                    fee_record = FeeRecord(student=student, month=month, year=year, amount=amount, due_date=today + timedelta(days=offset_value), due_date_offset=offset_value, late_fee_per_day=penalty_value, status='pending')
                    fee_record.save(force_insert=True)
                    created = True
                else:
                    created = False
            else:
                raise

        if not created:
            if fee_record.paid_amount > 0:
                return JsonResponse({'error': 'Fee already paid, cannot modify.'}, status=400)
            fee_record.extra_charges = effective_charges
            fee_record.due_date_offset = offset_value
            fee_record.late_fee_per_day = penalty_value
            if fee_record.due_date is None:
                fee_record.due_date = today + timedelta(days=offset_value)
            else:
                fee_record.due_date = fee_record.due_date
            if fee_record.paid_amount == 0 and fee_record.due_date < today and fee_record.status in ['pending', 'overdue']:
                fee_record.amount = max(amount + penalty_value, amount)
            else:
                fee_record.amount = amount
            fee_record.save()
        else:
            fee_record.extra_charges = effective_charges
            fee_record.due_date = today + timedelta(days=offset_value)
            fee_record.due_date_offset = offset_value
            fee_record.late_fee_per_day = penalty_value
            if fee_record.paid_amount == 0 and fee_record.due_date < today and fee_record.status in ['pending', 'overdue']:
                fee_record.amount = max(amount + penalty_value, amount)
            else:
                fee_record.amount = amount
            fee_record.save()

        if save_default:
            settings.default_extra_charges = effective_charges
            settings.due_date_offset = offset_value
            settings.late_fee_penalty = penalty_value
            settings.save(update_fields=['default_extra_charges', 'due_date_offset', 'late_fee_penalty'])

        voucher = {
            'receipt_number': f'VOUCHER-{student.id}-{year}{month:02d}',
            'student_name': student.name,
            'student_roll': student.roll_number,
            'grade': student.grade,
            'section': student.section,
            'fee_amount': float(amount),
            'charges': effective_charges,
            'month': month,
            'year': year,
            'due_date': fee_record.due_date.strftime('%Y-%m-%d'),
            'due_date_offset': offset_value,
            'late_fee_penalty': float(penalty_value),
            'total_pending': float(student.fee_records.aggregate(Sum('amount'))['amount__sum'] or 0) - float(student.payments.aggregate(Sum('amount'))['amount__sum'] or 0),
            'generated_on': timezone.now().strftime('%Y-%m-%d %H:%M'),
        }
        voucher['total'] = voucher['fee_amount'] + sum(ch['amount'] for ch in voucher['charges'])

        return JsonResponse({'success': True, 'voucher': voucher, 'created': created})




def voucher_html_api(request, schema_name, student_id):
    """API: Return HTML of the voucher for the current month (or latest if not exists)."""
    from django.http import HttpResponse
    from django.utils import timezone
    from django.template.loader import render_to_string
    from .models import Student, FeeRecord, SchoolClient, SchoolFeeSettings
    from django_tenants.utils import schema_context
    from decimal import Decimal

    with schema_context(schema_name):
        try:
            student = Student.objects.get(id=student_id)
        except Student.DoesNotExist:
            return HttpResponse('Student not found', status=404)

        today = timezone.localdate()
        month, year = today.month, today.year
        fee_record = FeeRecord.objects.filter(student=student, month=month, year=year).first()
        if not fee_record:
            return HttpResponse('No fee record for current month', status=404)

        tenant = None
        try:
            with schema_context('public'):
                tenant = SchoolClient.objects.get(schema_name=schema_name)
        except SchoolClient.DoesNotExist:
            pass

        settings, _ = SchoolFeeSettings.objects.get_or_create(pk=1)
        charges = fee_record.extra_charges or []
        total = fee_record.amount + sum(Decimal(str(ch['amount'])) for ch in charges)
        voucher_data = {
            'student': student,
            'fee_record': fee_record,
            'charges': charges,
            'total': total,
            'tenant': tenant,
            'settings': settings,
        }
        html = render_to_string('tenant/voucher_snippet.html', voucher_data)
        return HttpResponse(html)




# ==================== VOUCHERS LIST (Central page) ====================



@require_tenant_type(['school'])
def vouchers_list(request, schema_name):
    """List all fee vouchers (fee records) with filters, grouped by status."""
    tenant = get_tenant(request, schema_name)
    with schema_context(schema_name):
        today = timezone.localdate()
        current_month = today.month
        current_year = today.year

        # Get filter params (month, year, search)
        month = request.GET.get('month')
        year = request.GET.get('year')
        search = request.GET.get('search', '').strip()

        if month:
            try:
                month = int(month)
                if month < 1 or month > 12:
                    month = current_month
            except ValueError:
                month = current_month
        else:
            month = current_month

        if year:
            try:
                year = int(year)
            except ValueError:
                year = current_year
        else:
            year = current_year

        # Compute last day of month
        from calendar import monthrange
        last_day = date(year, month, monthrange(year, month)[1])

        # Fetch fee records for selected month/year
        fee_records_qs = FeeRecord.objects.filter(month=month, year=year).select_related('student').order_by('student__name')
        if search:
            fee_records_qs = fee_records_qs.filter(
                Q(student__name__icontains=search) |
                Q(student__roll_number__icontains=search) |
                Q(student__father_name__icontains=search) |
                Q(student__grade__icontains=search) |
                Q(student__section__icontains=search)
            )

        # Missing students: active students who existed in that month and have no fee record
        existing_student_ids = fee_records_qs.values_list('student_id', flat=True)
        missing_students = Student.objects.filter(
            status='active',
            enrolled_on__lte=last_day
        ).exclude(id__in=existing_student_ids)

        # Build lists for each status
        pending_items = []
        partial_items = []
        paid_items = []
        missing_items = []

        for fr in fee_records_qs:
            item = {
                'type': 'record',
                'record': fr,
                'student': fr.student,
                'status': fr.status,
                'amount': fr.amount,
                'paid': fr.paid_amount,
                'due_date': fr.due_date,
                'month': fr.month,
                'year': fr.year,
                'total_amount': fr.total_amount,
                'remaining': fr.remaining_total,
            }
            if fr.status == 'pending':
                pending_items.append(item)
            elif fr.status == 'partial':
                partial_items.append(item)
            elif fr.status == 'paid':
                paid_items.append(item)
            # other statuses like 'overdue' can be treated as pending? We'll keep them as pending
            else:
                pending_items.append(item)   # fallback

        for student in missing_students:
            reason = "No fee structure"
            if student.custom_fee > 0:
                reason = "Custom fee not set"
            else:
                fee_struct = FeeStructure.objects.filter(grade=student.grade).first()
                if fee_struct:
                    reason = "Not generated"
            missing_items.append({
                'type': 'missing',
                'student': student,
                'reason': reason,
                'status': 'missing',
                'amount': 0,
                'paid': 0,
                'due_date': None,
                'month': month,
                'year': year,
            })

        # Paginate each list separately
        per_page = 10
        pending_page = Paginator(pending_items, per_page).get_page(request.GET.get('pending_page', 1))
        partial_page = Paginator(partial_items, per_page).get_page(request.GET.get('partial_page', 1))
        paid_page = Paginator(paid_items, per_page).get_page(request.GET.get('paid_page', 1))
        missing_page = Paginator(missing_items, per_page).get_page(request.GET.get('missing_page', 1))

        context = {
            'tenant': tenant,
            'pending_page': pending_page,
            'partial_page': partial_page,
            'paid_page': paid_page,
            'missing_page': missing_page,
            'month': month,
            'year': year,
            'months': list(range(1, 13)),
            'years': list(range(current_year - 5, current_year + 2)),
            'search_query': search,
            'is_current_month': (month == current_month and year == current_year),
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        }
        return render(request, 'tenant/vouchers.html', context)





@require_tenant_type(['school'])
def mobile_vouchers_list(request, schema_name):
    """Mobile version of vouchers list grouped by status."""
    tenant = get_tenant(request, schema_name)
    with schema_context(schema_name):
        today = timezone.localdate()
        current_month = today.month
        current_year = today.year

        month = request.GET.get('month')
        year = request.GET.get('year')
        search = request.GET.get('search', '').strip()

        if month:
            try:
                month = int(month)
                if month < 1 or month > 12:
                    month = current_month
            except ValueError:
                month = current_month
        else:
            month = current_month

        if year:
            try:
                year = int(year)
            except ValueError:
                year = current_year
        else:
            year = current_year

        from calendar import monthrange
        last_day = date(year, month, monthrange(year, month)[1])

        fee_records_qs = FeeRecord.objects.filter(month=month, year=year).select_related('student').order_by('student__name')
        if search:
            fee_records_qs = fee_records_qs.filter(
                Q(student__name__icontains=search) |
                Q(student__roll_number__icontains=search) |
                Q(student__father_name__icontains=search) |
                Q(student__grade__icontains=search) |
                Q(student__section__icontains=search)
            )

        existing_student_ids = fee_records_qs.values_list('student_id', flat=True)
        missing_students = Student.objects.filter(
            status='active',
            enrolled_on__lte=last_day
        ).exclude(id__in=existing_student_ids)

        pending_items = []
        partial_items = []
        paid_items = []
        missing_items = []

        for fr in fee_records_qs:
            item = {
                'type': 'record',
                'record': fr,
                'student': fr.student,
                'status': fr.status,
                'amount': fr.amount,
                'paid': fr.paid_amount,
                'due_date': fr.due_date,
                'month': fr.month,
                'year': fr.year,
                'total_amount': fr.total_amount,
                'remaining': fr.remaining_total,
            }
            if fr.status == 'pending':
                pending_items.append(item)
            elif fr.status == 'partial':
                partial_items.append(item)
            elif fr.status == 'paid':
                paid_items.append(item)
            else:
                pending_items.append(item)

        for student in missing_students:
            reason = "No fee structure"
            if student.custom_fee > 0:
                reason = "Custom fee not set"
            else:
                fee_struct = FeeStructure.objects.filter(grade=student.grade).first()
                if fee_struct:
                    reason = "Not generated"
            missing_items.append({
                'type': 'missing',
                'student': student,
                'reason': reason,
                'status': 'missing',
                'amount': 0,
                'paid': 0,
                'due_date': None,
                'month': month,
                'year': year,
            })

        per_page = 10
        pending_page = Paginator(pending_items, per_page).get_page(request.GET.get('pending_page', 1))
        partial_page = Paginator(partial_items, per_page).get_page(request.GET.get('partial_page', 1))
        paid_page = Paginator(paid_items, per_page).get_page(request.GET.get('paid_page', 1))
        missing_page = Paginator(missing_items, per_page).get_page(request.GET.get('missing_page', 1))

        context = {
            'tenant': tenant,
            'pending_page': pending_page,
            'partial_page': partial_page,
            'paid_page': paid_page,
            'missing_page': missing_page,
            'month': month,
            'year': year,
            'months': list(range(1, 13)),
            'years': list(range(current_year - 5, current_year + 2)),
            'search_query': search,
            'is_current_month': (month == current_month and year == current_year),
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        }
        return render(request, 'mobile/vouchers.html', context)


# ------------------- Fee Logs View -------------------


def fee_logs(request, schema_name):
    """Display fee generation logs with filters."""
    from django.shortcuts import render
    from django.core.paginator import Paginator
    from .models import ManualGenerationLog
    from django_tenants.utils import schema_context

    tenant = get_tenant(request, schema_name)
    month = request.GET.get('month')
    year = request.GET.get('year')
    log_type = request.GET.get('log_type', '')
    page = request.GET.get('page', 1)

    with schema_context(schema_name):
        logs_qs = ManualGenerationLog.objects.all()
        if month:
            logs_qs = logs_qs.filter(month=month)
        if year:
            logs_qs = logs_qs.filter(year=year)
        if log_type:
            logs_qs = logs_qs.filter(log_type=log_type)
        logs_qs = logs_qs.order_by('-generated_at')

        paginator = Paginator(logs_qs, 20)
        logs_page = paginator.get_page(page)

        # Get distinct months/years for filter dropdowns
        months = range(1, 13)
        years = list(range(2020, date.today().year + 2))
        log_types = ManualGenerationLog.LOG_TYPE_CHOICES

        # For each log, compute a link to the voucher page for that month/year
        # The voucher page uses month/year filters
        for log in logs_page:
            log.voucher_url = f'/portal/{schema_name}/vouchers/mobile/?month={log.month}&year={log.year}&tab=pending'

        context = {
            'tenant': tenant,
            'logs': logs_page,
            'months': months,
            'years': years,
            'log_types': log_types,
            'selected_month': month,
            'selected_year': year,
            'selected_log_type': log_type,
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        }
        return render(request, 'tenant/fee_logs.html', context)





def mobile_fee_logs(request, schema_name):
    """Mobile version of fee logs."""
    from django.shortcuts import render
    from django.core.paginator import Paginator
    from .models import ManualGenerationLog
    from datetime import date
    from django_tenants.utils import schema_context

    tenant = get_tenant(request, schema_name)
    month = request.GET.get('month')
    year = request.GET.get('year')
    log_type = request.GET.get('log_type', '')
    page = request.GET.get('page', 1)

    with schema_context(schema_name):
        logs_qs = ManualGenerationLog.objects.all()
        if month:
            logs_qs = logs_qs.filter(month=month)
        if year:
            logs_qs = logs_qs.filter(year=year)
        if log_type:
            logs_qs = logs_qs.filter(log_type=log_type)
        logs_qs = logs_qs.order_by('-generated_at')

        paginator = Paginator(logs_qs, 20)
        logs_page = paginator.get_page(page)

        months = range(1, 13)
        years = list(range(2020, date.today().year + 2))
        log_types = ManualGenerationLog.LOG_TYPE_CHOICES

        for log in logs_page:
            log.voucher_url = f'/portal/{schema_name}/vouchers/mobile/?month={log.month}&year={log.year}&tab=pending'

        context = {
            'tenant': tenant,
            'logs': logs_page,
            'months': months,
            'years': years,
            'log_types': log_types,
            'selected_month': month,
            'selected_year': year,
            'selected_log_type': log_type,
            'logo_url': tenant.school_logo.url if tenant.school_logo else None,
        }
    return render(request, 'mobile/fee_logs.html', context)


# ---- Dismiss notification ----


@require_tenant_type(['school'])
def dismiss_notification(request, schema_name):
    from django.http import JsonResponse
    from .models import FeeRecord
    from django.db.models import Max
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        current_max_id = FeeRecord.objects.aggregate(Max('id'))['id__max'] or 0
        request.session['last_fee_record_id'] = current_max_id
        return JsonResponse({'status': 'ok'})


# ==================== NOTIFICATION API VIEWS ====================


@require_tenant_type(['school'])
def notifications_list_api(request, schema_name):
    """Return list of notifications for the tenant, with unread count."""
    from django.http import JsonResponse
    from .models import Notification
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        notifs = Notification.objects.all()[:50]
        unread_count = Notification.objects.filter(is_read=False).count()
        data = [{
            'id': n.id,
            'message': n.message,
            'link': n.link,
            'is_read': n.is_read,
            'created_at': n.created_at.isoformat(),
        } for n in notifs]
        return JsonResponse({'notifications': data, 'unread_count': unread_count})



@csrf_exempt
@require_tenant_type(['school'])
def mark_notification_read_api(request, schema_name):
    """Mark a single notification as read."""
    from django.http import JsonResponse
    from .models import Notification
    from django_tenants.utils import schema_context
    import json
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    try:
        data = json.loads(request.body)
        notif_id = data.get('id')
        if not notif_id:
            return JsonResponse({'error': 'Missing id'}, status=400)
        with schema_context(schema_name):
            notif = Notification.objects.get(id=notif_id)
            notif.is_read = True
            notif.save()
            return JsonResponse({'success': True})
    except Notification.DoesNotExist:
        return JsonResponse({'error': 'Notification not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)



@csrf_exempt
@require_tenant_type(['school'])
def mark_all_notifications_read_api(request, schema_name):
    """Mark all notifications as read for the tenant."""
    from django.http import JsonResponse
    from .models import Notification
    from django_tenants.utils import schema_context
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    with schema_context(schema_name):
        updated = Notification.objects.filter(is_read=False).update(is_read=True)
        return JsonResponse({'success': True, 'updated': updated})


# ==================== GLOBAL SEARCH API ====================


@require_tenant_type(['school'])
def global_search_api(request, schema_name):
    mobile = request.GET.get('mobile') == '1'
    """API endpoint for global search across students, fees, payments, products, categories."""
    from django.db.models import Q
    from .models import Student, FeeRecord, PaymentTransaction, Product, ProductCategory
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse({'error': 'Query too short'}, status=400)

    results = {}

    # Search Students
    students = Student.objects.filter(
        Q(name__icontains=q) |
        Q(roll_number__icontains=q) |
        Q(father_name__icontains=q) |
        Q(father_cnic__icontains=q) |
        Q(parent_mobile__icontains=q)
    )[:10]
    results['students'] = [{
        'title': s.name,
        'subtitle': f"Roll: {s.roll_number} | {s.grade} - {s.section}",
        'type': 'Student',
        'url': f"/portal/{schema_name}/students/{'mobile/' if mobile else ''}{s.id}/",
    } for s in students]

    # Search FeeRecords (by student name, month/year)
    fees = FeeRecord.objects.filter(
        Q(student__name__icontains=q) |
        Q(month__icontains=q) |
        Q(year__icontains=q)
    )[:10]
    results['fees'] = [{
        'title': f"{fr.student.name} - {fr.month}/{fr.year}",
        'subtitle': f"Amount: ₹{fr.total_amount} | Status: {fr.get_status_display()}",
        'type': 'Fee',
        'url': f"/portal/{schema_name}/students/{'mobile/' if mobile else ''}{fr.student.id}/",
    } for fr in fees]

    # Search Payments (by receipt number, student name)
    payments = PaymentTransaction.objects.filter(
        Q(receipt_number__icontains=q) |
        Q(student__name__icontains=q)
    )[:10]
    results['payments'] = [{
        'title': p.receipt_number,
        'subtitle': f"{p.student.name} - ₹{p.amount} on {p.payment_date}",
        'type': 'Payment',
        'url': f"/portal/{schema_name}/fee/receipt/{'mobile/' if mobile else ''}{p.id}/",
    } for p in payments]

    # Search Products
    products = Product.objects.filter(
        Q(name__icontains=q) |
        Q(sku__icontains=q)
    )[:10]
    results['products'] = [{
        'title': p.name,
        'subtitle': f"SKU: {p.sku} | Price: ₹{p.selling_price} | Stock: {p.quantity}",
        'type': 'Product',
        'url': f"/portal/{schema_name}/stock/product/{p.id}/{'mobile/' if mobile else ''}",
    } for p in products]

    # Search Categories
    categories = ProductCategory.objects.filter(name__icontains=q)[:10]
    results['categories'] = [{
        'title': c.name,
        'subtitle': c.description or '',
        'type': 'Category',
        'url': f"/portal/{schema_name}/stock/{'mobile/' if mobile else ''}?category={c.id}",
    } for c in categories]

    return JsonResponse(results)


def product_list_api(request, schema_name):
    """API: Return list of products with their detail URLs for pre‑caching."""
    from django.http import JsonResponse
    from .models import Product
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        products = Product.objects.all().values('id', 'name')
        # Build URLs for both desktop and mobile
        data = []
        for p in products:
            data.append({
                'id': p['id'],
                'desktop_url': f'/portal/{schema_name}/stock/product/{p["id"]}/',
                'mobile_url': f'/portal/{schema_name}/stock/product/{p["id"]}/mobile/',
            })
        return JsonResponse(data, safe=False)


def student_list_api(request, schema_name):
    """API: Return list of students with their profile URLs for pre‑caching."""
    from django.http import JsonResponse
    from .models import Student
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        students = Student.objects.filter(status='active').values('id', 'name')
        data = []
        for s in students:
            data.append({
                'id': s['id'],
                'desktop_url': f'/portal/{schema_name}/students/{s["id"]}/',
                'mobile_url': f'/portal/{schema_name}/students/{s["id"]}/mobile/',
            })
        return JsonResponse(data, safe=False)


def receipt_list_api(request, schema_name):
    """API: Return list of all receipt URLs (fee + gym) for pre‑caching."""
    from django.http import JsonResponse
    from .models import PaymentTransaction, GymPayment
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        data = []
        # Fee receipts
        for p in PaymentTransaction.objects.all().only('id'):
            data.append({
                'desktop_url': f'/portal/{schema_name}/fee/receipt/{p.id}/',
                'mobile_url': f'/portal/{schema_name}/fee/receipt/mobile/{p.id}/',
            })
        # Gym receipts
        for p in GymPayment.objects.all().only('id'):
            data.append({
                'desktop_url': f'/portal/{schema_name}/gym/receipt/{p.id}/',
                'mobile_url': f'/portal/{schema_name}/gym/receipt/{p.id}/',  # same as desktop (no separate mobile)
            })
        return JsonResponse(data, safe=False)


def fee_collection_list_api(request, schema_name):
    """API: Return list of active students with their fee collection URLs for pre‑caching."""
    from django.http import JsonResponse
    from .models import Student
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        students = Student.objects.filter(status='active').values('id', 'name')
        data = []
        for s in students:
            data.append({
                'id': s['id'],
                'desktop_url': f'/portal/{schema_name}/fee/collection/{s["id"]}/',
                'mobile_url': f'/portal/{schema_name}/fee/collection/mobile/{s["id"]}/',
            })
        return JsonResponse(data, safe=False)

