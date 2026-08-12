from django.contrib import admin
from django.urls import path, re_path, include
from django.conf import settings as django_settings
from django.conf.urls.static import static
from django.http import HttpResponse, Http404, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.http import Http404
from django_tenants.utils import schema_context

from .models import SchoolClient

from .views import mobile_fee_structure, gym_generate_subscription, gym_cancel_subscription, gym_update_subscription, gym_edit_attendance, add_student, add_student_mobile, dashboard, debug_payments_api, defaulters, edit_student, family_payment, fee_collection, mobile_fee_collection, fee_receipt, mobile_fee_receipt, fee_settings, fee_status_api, fee_structure, gym_attendance, gym_checkin_api, gym_checkout_api, gym_customer_add, gym_customer_edit, gym_customer_list, gym_customer_profile, gym_dashboard, gym_payment, gym_receipt, gym_reports, gym_settings, manual_generate_api, manual_generate_single_api, reports, settings, student_fee_records_api, student_list, student_payments_api, student_current_fee_status_api, student_profile, student_search_api, gym_revenue_stats_api, gym_attendance_stats_api, gym_customers_list_api, gym_customer_detail_api, gym_subscription_status_api, gym_attendance_data_api, gym_eligible_customers_api, gym_search_customer_api, gym_export_attendance_api, stock_management, product_detail, mobile_stock_management, mobile_product_detail, add_category, delete_category, add_product, delete_product, sell_separately, mobile_sell_separately, mobile_dashboard, mobile_more, mobile_student_list, mobile_student_profile, mobile_defaulters, mobile_reports, mobile_fee_settings, mobile_settings, vouchers_list, mobile_vouchers_list, dismiss_notification, notifications_list_api, mark_notification_read_api, mark_all_notifications_read_api, global_search_api, product_list_api, student_list_api, receipt_list_api, fee_collection_list_api, sync_offline_student_api, sync_offline_payment_api
from .pwa_views import manifest, service_worker
from .views import voucher_status_api, generate_voucher_api, voucher_html_api, global_search_api
from .views import fee_logs, mobile_fee_logs, global_search_api



def saas_homepage(request):
    return HttpResponse('''
    <h1>AXIS School Management System</h1>
    <p>Welcome to Multi-Tenant Platform</p>
    <p>Go to <a href="/admin/">Admin Panel</a> to manage schools</p>
    ''')

def ensure_schoolclient(schema_name):
    """Fetch tenant from public schema; raise 404 if not found."""
    with schema_context('public'):
        try:
            return SchoolClient.objects.get(schema_name=schema_name)
        except SchoolClient.DoesNotExist:
            raise Http404(f"Tenant '{schema_name}' does not exist.")

        if schema_exists:
            # Create the missing SchoolClient row
            tenant, created = SchoolClient.objects.get_or_create(
                schema_name=schema_name,
                defaults={
                    'name': f"{schema_name.title()} School",
                    'admin_username': 's',
                    'admin_password': 'admin123',
                    'is_active': True
                }
            )
            if created:
                print(f"✅ Auto-created SchoolClient for '{schema_name}'")
            return tenant
        else:
            return None

def portal_wrapper(view_func):
    """Wrapper that ensures SchoolClient exists before calling the view."""
    def wrapper(request, schema_name, *args, **kwargs):
        tenant = ensure_schoolclient(schema_name)
        if tenant is None:
            raise Http404(f"Tenant schema '{schema_name}' does not exist.")
        # Store tenant in request for convenience
        request.tenant = tenant
        return view_func(request, schema_name, *args, **kwargs)
    return wrapper

def get_school_default_route(tenant):
    mapping = [
        ('dashboard', 'dashboard'),
        ('students', 'student_list'),
        ('fee_collection', 'fee_collection'),
        ('defaulters', 'defaulters'),
        ('reports', 'reports'),
        ('stock_management', 'stock_management'),
        ('fee_structure', 'fee_structure'),
        ('fee_settings', 'fee_settings'),
        ('family_payment', 'family_payment'),
    ]
    if tenant.tenant_type != 'school':
        return 'dashboard'
    for feature, route_name in mapping:
        if tenant.is_feature_enabled(feature):
            return route_name
    return 'settings'


def school_login(request, schema_name):
    # Ensure tenant exists
    tenant = ensure_schoolclient(schema_name)
    if tenant is None:
        raise Http404(f"Tenant schema '{schema_name}' does not exist.")

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        if username == tenant.admin_username and password == tenant.admin_password:
            request.session['school_admin_authenticated'] = True
            request.session['school_admin_schema'] = tenant.schema_name
            request.session['school_admin_username'] = username
            request.session.save()   # Force session save immediately
            # Redirect to appropriate dashboard based on tenant_type
            if tenant.tenant_type == 'gym':
                return redirect('gym_dashboard', schema_name=tenant.schema_name)
            else:
                return redirect(get_school_default_route(tenant), schema_name=tenant.schema_name)
        return render(request, 'tenant/login.html', {'tenant': tenant, 'error': 'Invalid credentials'})
    return render(request, 'tenant/login.html', {'tenant': tenant})

def school_logout(request, schema_name):
    request.session.flush()
    return redirect('school_login', schema_name=schema_name)

def login_required_for_schema(view_func):
    def wrapper(request, schema_name, *args, **kwargs):
        if not request.session.get('school_admin_authenticated') or request.session.get('school_admin_schema') != schema_name:
            return redirect('school_login', schema_name=schema_name)
        return view_func(request, schema_name, *args, **kwargs)
    return wrapper

# Wrap all portal views with portal_wrapper to ensure SchoolClient exists
dashboard_view = portal_wrapper(login_required_for_schema(dashboard))
mobile_dashboard_view = portal_wrapper(login_required_for_schema(mobile_dashboard))
mobile_more_view = portal_wrapper(login_required_for_schema(mobile_more))
mobile_student_list_view = portal_wrapper(login_required_for_schema(mobile_student_list))
mobile_student_profile_view = portal_wrapper(login_required_for_schema(mobile_student_profile))
student_list_view = portal_wrapper(login_required_for_schema(student_list))
student_profile_view = portal_wrapper(login_required_for_schema(student_profile))
fee_collection_view = portal_wrapper(login_required_for_schema(fee_collection))
mobile_fee_collection_view = portal_wrapper(login_required_for_schema(mobile_fee_collection))
fee_receipt_view = portal_wrapper(login_required_for_schema(fee_receipt))
mobile_fee_receipt_view = portal_wrapper(login_required_for_schema(mobile_fee_receipt))
defaulters_view = portal_wrapper(login_required_for_schema(defaulters))
reports_view = portal_wrapper(login_required_for_schema(reports))
settings_view = portal_wrapper(login_required_for_schema(settings))
fee_structure_view = portal_wrapper(login_required_for_schema(fee_structure))
mobile_fee_structure_view = portal_wrapper(login_required_for_schema(mobile_fee_structure))
fee_settings_view = portal_wrapper(login_required_for_schema(fee_settings))
family_payment_view = portal_wrapper(login_required_for_schema(family_payment))
student_search_api_view = portal_wrapper(login_required_for_schema(student_search_api))
add_student_view = portal_wrapper(login_required_for_schema(add_student))
add_student_mobile_view = portal_wrapper(login_required_for_schema(add_student_mobile))
edit_student_view = portal_wrapper(login_required_for_schema(edit_student))
student_fee_records_api_view = portal_wrapper(login_required_for_schema(student_fee_records_api))
student_payments_api_view = portal_wrapper(login_required_for_schema(student_payments_api))
student_current_fee_status_api_view = portal_wrapper(login_required_for_schema(student_current_fee_status_api))
gym_dashboard_view = portal_wrapper(login_required_for_schema(gym_dashboard))
gym_customer_list_view = portal_wrapper(login_required_for_schema(gym_customer_list))
gym_customer_add_view = portal_wrapper(login_required_for_schema(gym_customer_add))
gym_customer_edit_view = portal_wrapper(login_required_for_schema(gym_customer_edit))
gym_customer_profile_view = portal_wrapper(login_required_for_schema(gym_customer_profile))
gym_attendance_view = portal_wrapper(login_required_for_schema(gym_attendance))
gym_payment_view = portal_wrapper(login_required_for_schema(gym_payment))
gym_reports_view = portal_wrapper(login_required_for_schema(gym_reports))
gym_settings_view = portal_wrapper(login_required_for_schema(gym_settings))
mobile_defaulters_view = portal_wrapper(login_required_for_schema(mobile_defaulters))
mobile_reports_view = portal_wrapper(login_required_for_schema(mobile_reports))
mobile_fee_settings_view = portal_wrapper(login_required_for_schema(mobile_fee_settings))
mobile_settings_view = portal_wrapper(login_required_for_schema(mobile_settings))



def api_gym_edit_attendance(request, attendance_id):
    """Wrapper for gym_edit_attendance that reads schema_name from session."""
    schema_name = request.session.get("school_admin_schema")
    if not schema_name:
        return JsonResponse({"error": "Unauthorized or no tenant"}, status=401)
    return gym_edit_attendance(request, schema_name, attendance_id)



def tenant_root_redirect(request, schema_name):
    """Redirect to appropriate dashboard based on tenant_type."""
    tenant = ensure_schoolclient(schema_name)
    if tenant is None:
        raise Http404("Tenant not found")
    if tenant.tenant_type == 'gym':
        return redirect('gym_dashboard', schema_name=schema_name)
    else:
        return redirect(get_school_default_route(tenant), schema_name=schema_name)


urlpatterns = [
    path('portal/<slug:schema_name>/api/fee-collection/', portal_wrapper(login_required_for_schema(fee_collection_list_api)), name='fee_collection_list_api'),
    path('portal/<slug:schema_name>/api/receipts/', portal_wrapper(login_required_for_schema(receipt_list_api)), name='receipt_list_api'),
    path('portal/<slug:schema_name>/api/students/', portal_wrapper(login_required_for_schema(student_list_api)), name='student_list_api'),
    path('portal/<slug:schema_name>/api/products/', portal_wrapper(login_required_for_schema(product_list_api)), name='product_list_api'),
    path('portal/<slug:schema_name>/api/student/<int:student_id>/current-fee-status/', student_current_fee_status_api_view, name='student_current_fee_status'),
    # ===== GYM ROUTES (FIXED ORDER) =====
    path('portal/<slug:schema_name>/gym/customers/<int:customer_id>/generate-subscription/', portal_wrapper(login_required_for_schema(gym_generate_subscription)), name='gym_generate_subscription'),
    path('portal/<slug:schema_name>/gym/subscriptions/<int:subscription_id>/cancel/', portal_wrapper(login_required_for_schema(gym_cancel_subscription)), name='gym_cancel_subscription'),
    path('portal/<slug:schema_name>/gym/subscriptions/<int:subscription_id>/update/', portal_wrapper(login_required_for_schema(gym_update_subscription)), name='gym_update_subscription'),
    path('portal/<slug:schema_name>/gym/attendance/<int:attendance_id>/edit/', portal_wrapper(login_required_for_schema(gym_edit_attendance)), name='gym_edit_attendance'),
    # Gym subscription & cancellation routes
    path('api/debug-payments/', debug_payments_api, name='debug_payments_api'),
    path('', saas_homepage),
    path('admin/', admin.site.urls),
    path('api/fee-status/', fee_status_api, name='fee_status_api'),
    path('api/manual-generate/', manual_generate_api, name='manual_generate_api'),
    path('api/manual-generate-single/', manual_generate_single_api, name='manual_generate_single_api'),
    
    # Auth
    path('portal/<slug:schema_name>/login/', school_login, name='school_login'),
    path('portal/<slug:schema_name>/login/', school_login, name='tenant_login'),
    path('portal/<slug:schema_name>/logout/', school_logout, name='school_logout'),
    path('portal/<slug:schema_name>/logout/', school_logout, name='tenant_logout'),
    
    # Core - using the wrapped views
    
    path('portal/<slug:schema_name>/dashboard/mobile/more/', mobile_more_view, name='mobile_more'),
    path('portal/<slug:schema_name>/dashboard/mobile/', mobile_dashboard_view, name='mobile_dashboard'),
    path('portal/<slug:schema_name>/students/mobile/', mobile_student_list_view, name='mobile_student_list'),
    path('portal/<slug:schema_name>/students/mobile/<int:student_id>/', mobile_student_profile_view, name='mobile_student_profile'),
    path('portal/<slug:schema_name>/fee/collection/mobile/', mobile_fee_collection_view, name='mobile_fee_collection'),
    path('portal/<slug:schema_name>/fee/collection/mobile/<int:student_id>/', mobile_fee_collection_view, name='mobile_fee_collection'),
    path('portal/<slug:schema_name>/fee/receipt/mobile/<int:receipt_id>/', mobile_fee_receipt_view, name='mobile_fee_receipt'),
    path('portal/<slug:schema_name>/dashboard/', dashboard_view, name='dashboard'),
    path('portal/<slug:schema_name>/students/', student_list_view, name='student_list'),
    path('portal/<slug:schema_name>/students/add/', add_student_view, name='add_student'),
    path('portal/<slug:schema_name>/students/add/mobile/', add_student_mobile_view, name='add_student_mobile'),
    path('portal/<slug:schema_name>/students/edit/<int:student_id>/', edit_student_view, name='edit_student'),
    path('portal/<slug:schema_name>/students/<int:student_id>/', student_profile_view, name='student_profile'),
    
    # Fee collection
    # Global Search API
    path('portal/<slug:schema_name>/api/global-search/', portal_wrapper(login_required_for_schema(global_search_api)), name='global_search_api'),
re_path(r'^portal/(?P<schema_name>[a-zA-Z0-9_-]+)/fee/collection/(?:(?P<student_id>\d+)/)?$', fee_collection_view, name='fee_collection'),
    path('portal/<slug:schema_name>/fee/receipt/<int:receipt_id>/', fee_receipt_view, name='fee_receipt'),
    path('portal/<slug:schema_name>/defaulters/', defaulters_view, name='defaulters'),
    path('portal/<slug:schema_name>/defaulters/mobile/', mobile_defaulters_view, name='mobile_defaulters'),
    path('portal/<slug:schema_name>/reports/', reports_view, name='reports'),
    path('portal/<slug:schema_name>/reports/mobile/', mobile_reports_view, name='mobile_reports'),
    path('portal/<slug:schema_name>/settings/', settings_view, name='settings'),
    path('portal/<slug:schema_name>/fee/structure/', fee_structure_view, name='fee_structure'),
    path('portal/<slug:schema_name>/fee/structure/mobile/', mobile_fee_structure_view, name='mobile_fee_structure'),
    path('portal/<slug:schema_name>/fee/settings/', fee_settings_view, name='fee_settings'),
    path('portal/<slug:schema_name>/fee/settings/mobile/', mobile_fee_settings_view, name='mobile_fee_settings'),
    path('portal/<slug:schema_name>/settings/mobile/', mobile_settings_view, name='mobile_settings'),
    path('portal/<slug:schema_name>/fee/family-payment/', family_payment_view, name='family_payment'),
    path('portal/<slug:schema_name>/api/student-search/', student_search_api_view, name='student_search_api'),
    path('portal/<slug:schema_name>/api/sync-offline-student/', portal_wrapper(login_required_for_schema(sync_offline_student_api)), name='sync_offline_student_api'),
    path('portal/<slug:schema_name>/api/sync-offline-payment/', portal_wrapper(login_required_for_schema(sync_offline_payment_api)), name='sync_offline_payment_api'),
    path('portal/<slug:schema_name>/api/student/<int:student_id>/fee-records/', student_fee_records_api_view, name='student_fee_records_api'),
    path('portal/<slug:schema_name>/api/student/<int:student_id>/payments/', student_payments_api_view, name='student_payments_api'),
    
    # Gym API
    path('api/gym/checkin/', gym_checkin_api, name='gym_checkin_api'),
    path('api/gym/checkout/', gym_checkout_api, name='gym_checkout_api'),
    path('portal/<slug:schema_name>/gym/receipt/<int:receipt_id>/', gym_receipt, name='gym_receipt'),

    # Gym routes
    path('portal/<slug:schema_name>/gym-dashboard/', gym_dashboard_view, name='gym_dashboard'),
    path('portal/<slug:schema_name>/gym/customers/', gym_customer_list_view, name='gym_customer_list'),
    path('portal/<slug:schema_name>/gym/customers/add/', gym_customer_add_view, name='gym_customer_add'),
    path('portal/<slug:schema_name>/gym/customers/edit/<int:customer_id>/', gym_customer_edit_view, name='gym_customer_edit'),
    path('portal/<slug:schema_name>/gym/customers/<int:customer_id>/', gym_customer_profile_view, name='gym_customer_profile'),
    path('portal/<slug:schema_name>/gym/attendance/', gym_attendance_view, name='gym_attendance'),
    path('portal/<slug:schema_name>/gym/payments/', gym_payment_view, name='gym_payment'),
    path('portal/<slug:schema_name>/gym/payments/<int:customer_id>/', gym_payment_view, name='gym_payment'),
    path('portal/<slug:schema_name>/gym/reports/', gym_reports_view, name='gym_reports'),
    
    # Gym Reports API endpoints
    path('api/gym/revenue-stats/<slug:schema_name>/', gym_revenue_stats_api, name='gym_revenue_stats_api'),
    path('api/gym/attendance-stats/<slug:schema_name>/', gym_attendance_stats_api, name='gym_attendance_stats_api'),
    path('api/gym/customers-list/<slug:schema_name>/', gym_customers_list_api, name='gym_customers_list_api'),
    path('api/gym/customer-detail/<slug:schema_name>/<int:customer_id>/', gym_customer_detail_api, name='gym_customer_detail_api'),
    path('api/gym/subscription-status/<slug:schema_name>/', gym_subscription_status_api, name='gym_subscription_status_api'),
    
    path('portal/<slug:schema_name>/gym/settings/', gym_settings_view, name='gym_settings'),

    path('api/gym/attendance-data/<slug:schema_name>/', gym_attendance_data_api, name='gym_attendance_data_api'),

    path('api/gym/eligible-customers/<slug:schema_name>/', gym_eligible_customers_api, name='gym_eligible_customers_api'),

    path('api/gym/search-customer/<slug:schema_name>/', gym_search_customer_api, name='gym_search_customer_api'),

    path('api/gym/export-attendance/<slug:schema_name>/', gym_export_attendance_api, name='gym_export_attendance_api'),

    path('api/gym/attendance/<int:attendance_id>/edit/', api_gym_edit_attendance, name='gym_edit_attendance_api'),
    path('portal/<slug:schema_name>/', tenant_root_redirect, name='tenant_root'),

    # ===== STOCK MANAGEMENT ROUTES =====
    path('portal/<slug:schema_name>/stock/', portal_wrapper(login_required_for_schema(stock_management)), name='stock_management'),
    path('portal/<slug:schema_name>/stock/product/<int:product_id>/', portal_wrapper(login_required_for_schema(product_detail)), name='product_detail'),
    path('portal/<slug:schema_name>/stock/category/add/', portal_wrapper(login_required_for_schema(add_category)), name='add_category'),
    path('portal/<slug:schema_name>/stock/category/delete/<int:category_id>/', portal_wrapper(login_required_for_schema(delete_category)), name='delete_category'),
    path('portal/<slug:schema_name>/stock/product/add/', portal_wrapper(login_required_for_schema(add_product)), name='add_product'),
    path('portal/<slug:schema_name>/stock/product/delete/<int:product_id>/', portal_wrapper(login_required_for_schema(delete_product)), name='delete_product'),
    # Mobile stock routes
    path('portal/<slug:schema_name>/stock/mobile/', portal_wrapper(login_required_for_schema(mobile_stock_management)), name='mobile_stock_management'),
    path('portal/<slug:schema_name>/stock/product/<int:product_id>/mobile/', portal_wrapper(login_required_for_schema(mobile_product_detail)), name='mobile_product_detail'),

    
    # ===== VOUCHERS (central listing) =====
    path('portal/<slug:schema_name>/vouchers/', portal_wrapper(login_required_for_schema(vouchers_list)), name='vouchers_list'),
    
    path('portal/<slug:schema_name>/fee/logs/', portal_wrapper(login_required_for_schema(fee_logs)), name='fee_logs'),
    path('portal/<slug:schema_name>/fee/logs/mobile/', portal_wrapper(login_required_for_schema(mobile_fee_logs)), name='mobile_fee_logs'),
    path('portal/<slug:schema_name>/api/dismiss-notification/', portal_wrapper(login_required_for_schema(dismiss_notification)), name='dismiss_notification'),
    path('portal/<slug:schema_name>/vouchers/mobile/', portal_wrapper(login_required_for_schema(mobile_vouchers_list)), name='mobile_vouchers_list'),

# ===== SELL SEPARATELY (standalone student search) =====
    path('portal/<slug:schema_name>/sell/', portal_wrapper(login_required_for_schema(sell_separately)), name='sell_separately'),
    path('portal/<slug:schema_name>/sell/mobile/', portal_wrapper(login_required_for_schema(mobile_sell_separately)), name='mobile_sell_separately'),
    path('sw.js', service_worker, name='service_worker'),
    path('portal/<slug:schema_name>/manifest.json', manifest, name='pwa_manifest'),

    # Voucher endpoints
    path('portal/<slug:schema_name>/api/student/<int:student_id>/voucher-status/', voucher_status_api, name='voucher_status_api'),
    path('portal/<slug:schema_name>/api/student/<int:student_id>/generate-voucher/', generate_voucher_api, name='generate_voucher_api'),
    path('portal/<slug:schema_name>/api/student/<int:student_id>/voucher-html/', voucher_html_api, name='voucher_html_api'),

    # Notification API endpoints
    path('portal/<slug:schema_name>/api/notifications/', portal_wrapper(login_required_for_schema(notifications_list_api)), name='notifications_list_api'),
    path('portal/<slug:schema_name>/api/notifications/mark-read/', portal_wrapper(login_required_for_schema(mark_notification_read_api)), name='mark_notification_read_api'),
    path('portal/<slug:schema_name>/api/notifications/mark-all-read/', portal_wrapper(login_required_for_schema(mark_all_notifications_read_api)), name='mark_all_notifications_read_api'),
]