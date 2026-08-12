// axis_saas/static/js/offline_payment.js
(function() {
    'use strict';

    const WAIT_INTERVAL_MS = 50;
    const WAIT_TIMEOUT_MS = 5000;
    const OFFLINE_SYNC_CHANNEL = 'axis-offline-sync';
    const PAYMENT_FORM_SELECTOR = '#paymentForm';
    const TEMP_RECEIPT_CLASS = 'offline-temp-receipt';
    const SYNC_ENDPOINT_PATH = '/portal/{schema}/api/sync-offline-payment/';
    const APPLIED_PAYMENTS = new Set();

    function getCookie(name) {
        const cookieValue = document.cookie.split('; ').find(row => row.startsWith(name + '='));
        return cookieValue ? decodeURIComponent(cookieValue.split('=')[1]) : null;
    }

    function getSchemaFromPath() {
        const parts = window.location.pathname.split('/').filter(Boolean);
        const portalIndex = parts.indexOf('portal');
        if (portalIndex >= 0 && parts.length > portalIndex + 1) {
            return parts[portalIndex + 1];
        }
        return null;
    }

    function parseCurrency(value) {
        if (!value && value !== 0) return 0;
        const cleaned = String(value).replace(/[^0-9.-]+/g, '');
        return parseFloat(cleaned) || 0;
    }

    function formatCurrency(value) {
        return '₹' + Number(value || 0).toFixed(2);
    }

    function waitForOfflineStudent() {
        return new Promise((resolve, reject) => {
            if (window.offlineStudent) {
                return resolve(window.offlineStudent);
            }
            let waited = 0;
            const interval = setInterval(() => {
                if (window.offlineStudent) {
                    clearInterval(interval);
                    return resolve(window.offlineStudent);
                }
                waited += WAIT_INTERVAL_MS;
                if (waited >= WAIT_TIMEOUT_MS) {
                    clearInterval(interval);
                    reject(new Error('offlineStudent helper did not become available in time'));
                }
            }, WAIT_INTERVAL_MS);
        });
    }

    function getCurrentStudentId() {
        const path = window.location.pathname;
        const match = path.match(/\/students\/(?:mobile\/)?(\d+)\/?$/) || path.match(/\/fee\/collection\/(?:mobile\/)?(\d+)\/?$/);
        return match ? Number(match[1]) : null;
    }

    function getPaymentForm() {
        return document.querySelector(PAYMENT_FORM_SELECTOR);
    }

    function buildPaymentPayload(form) {
        const formData = new FormData(form);
        const studentId = Number(formData.get('student_id')) || getCurrentStudentId();
        const amount = parseCurrency(formData.get('amount'));
        const paymentMode = formData.get('payment_mode') || 'cash';
        const productItemsRaw = formData.get('product_items_json') || '[]';
        let productItems = [];
        try {
            productItems = JSON.parse(productItemsRaw) || [];
        } catch (err) {
            productItems = [];
        }

        return {
            student_id: studentId,
            amount: amount,
            payment_mode: paymentMode,
            remarks: 'Offline payment saved locally and will sync when online.',
            product_items: productItems,
            temp_receipt: `OFFLINE-${Date.now()}-${Math.random().toString(36).slice(2, 10).toUpperCase()}`
        };
    }

    function getTotalItemAmount(productItems) {
        if (!Array.isArray(productItems) || productItems.length === 0) {
            return 0;
        }
        let total = 0;
        productItems.forEach(item => {
            const card = document.querySelector(`.item-card[data-product-id="${item.product_id}"]`);
            const price = card ? parseCurrency(card.dataset.price) : 0;
            const qty = Number(item.quantity) || 0;
            total += price * qty;
        });
        return total;
    }

    function updateElementValue(el, delta) {
        if (!el) return;
        const current = parseCurrency(el.textContent || el.value);
        const updated = Math.max(current + delta, 0);
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
            el.value = formatCurrency(updated);
        } else {
            el.textContent = formatCurrency(updated);
        }
    }

    function updatePendingSummary(delta) {
        const summaryElements = [
            document.getElementById('totalPending'),
            document.getElementById('pendingTotal'),
            document.getElementById('pendingDisplay'),
            document.querySelector('.pending-badge'),
            document.querySelector('.summary-value.pending'),
            document.querySelector('.pending-amount')
        ];
        summaryElements.forEach(el => {
            if (!el) return;
            if (el.classList.contains('pending-amount') && el.textContent) {
                updateElementValue(el, -delta);
                return;
            }
            if (el.id === 'pendingDisplay' || el.id === 'pendingTotal' || el.id === 'totalPending') {
                updateElementValue(el, -delta);
            }
        });
    }

    function applyAmountToPendingRows(amountToApply) {
        if (!amountToApply || amountToApply <= 0) return amountToApply;
        const rows = Array.from(document.querySelectorAll('.pending-table-section tbody tr')).filter(row => {
            return !row.classList.contains('total-row') && row.id !== 'noPendingRow';
        });
        let remainingAmount = amountToApply;
        rows.forEach(row => {
            if (remainingAmount <= 0) return;
            const remainingCell = row.querySelector('.remaining');
            if (!remainingCell) return;
            const currentRemaining = parseCurrency(remainingCell.textContent);
            if (currentRemaining <= 0) return;
            const applied = Math.min(currentRemaining, remainingAmount);
            const newRemaining = Math.max(currentRemaining - applied, 0);
            remainingCell.textContent = formatCurrency(newRemaining);
            remainingAmount -= applied;
            if (newRemaining === 0) {
                remainingCell.classList.add('paid');
            }
        });
        return remainingAmount;
    }

    function insertOfflinePaymentHistory(payment) {
        const tables = Array.from(document.querySelectorAll('table'));
        for (const table of tables) {
            const headings = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim().toLowerCase());
            if (!headings.includes('receipt') || !headings.includes('amount')) continue;
            const tbody = table.querySelector('tbody');
            if (!tbody) continue;
            const row = document.createElement('tr');
            row.className = 'offline-payment-row';
            const receiptCell = document.createElement('td');
            receiptCell.innerHTML = `<code data-temp-receipt="${payment.temp_receipt}" class="${TEMP_RECEIPT_CLASS}">${payment.temp_receipt}</code> <span class="sync-badge">Pending sync</span>`;
            const amountCell = document.createElement('td');
            amountCell.textContent = formatCurrency(payment.amount);
            const studentCell = document.createElement('td');
            studentCell.textContent = `Student ID ${payment.student_id}`;
            const dateCell = document.createElement('td');
            dateCell.textContent = new Date(payment.created_at).toLocaleDateString();
            const modeCell = document.createElement('td');
            modeCell.textContent = payment.payment_mode || 'cash';
            const actionCell = document.createElement('td');
            actionCell.innerHTML = '<span style="color:#f59e0b;font-weight:700;">Offline</span>';
            row.appendChild(receiptCell);
            row.appendChild(studentCell);
            row.appendChild(amountCell);
            row.appendChild(dateCell);
            row.appendChild(modeCell);
            row.appendChild(actionCell);
            tbody.prepend(row);
            return;
        }
    }

    function showBanner(message, type = 'success') {
        const existing = document.querySelector('.offline-payment-banner');
        if (existing) existing.remove();
        const banner = document.createElement('div');
        banner.className = 'offline-payment-banner';
        banner.style.cssText = 'padding:1rem 1.25rem;margin:1rem 0;border-radius:0.75rem;font-weight:600;display:flex;align-items:center;justify-content:space-between;gap:1rem;line-height:1.3;';
        banner.style.background = type === 'error' ? '#fee2e2' : '#ecfdf5';
        banner.style.color = type === 'error' ? '#991b1b' : '#166534';
        banner.innerHTML = `<span>${message}</span><button type="button" style="background:transparent;border:0;color:inherit;font-weight:700;cursor:pointer;">Dismiss</button>`;
        banner.querySelector('button').addEventListener('click', () => banner.remove());
        const container = document.querySelector('.page-header, .page-hero, .student-info-card, .student-info, body');
        if (container) {
            container.parentNode.insertBefore(banner, container.nextSibling);
        } else {
            document.body.prepend(banner);
        }
    }

    function showToast(message) {
        const existing = document.querySelector('.offline-toast');
        if (existing) existing.remove();
        const toast = document.createElement('div');
        toast.className = 'offline-toast';
        toast.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#047857;color:white;padding:0.85rem 1.1rem;border-radius:999px;box-shadow:0 10px 28px rgba(0,0,0,0.18);z-index:9999;font-weight:700;font-size:0.95rem;';
        toast.textContent = message;
        document.body.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transition = 'opacity 0.25s ease';
            setTimeout(() => toast.remove(), 250);
        }, 3800);
    }

    function replaceTempReceiptElements(tempReceipt, receiptNumber) {
        const spanSelector = `[data-temp-receipt="${tempReceipt}"]`;
        const elements = Array.from(document.querySelectorAll(spanSelector));
        elements.forEach(el => {
            el.textContent = receiptNumber;
            el.classList.remove(TEMP_RECEIPT_CLASS);
            el.removeAttribute('data-temp-receipt');
        });
    }

    function getPaymentEndpoint(schemaName) {
        return SYNC_ENDPOINT_PATH.replace('{schema}', schemaName);
    }

    function applyOfflinePaymentToUI(payment, sourceIsSave = false) {
        if (!payment || APPLIED_PAYMENTS.has(payment.temp_receipt)) {
            return;
        }
        APPLIED_PAYMENTS.add(payment.temp_receipt);
        const itemTotal = getTotalItemAmount(payment.product_items);
        const currentPending = parseCurrency(document.getElementById('totalPending')?.textContent || document.getElementById('pendingDisplay')?.textContent || document.getElementById('pendingTotal')?.textContent);
        const feeApplied = Math.min(payment.amount, currentPending);
        const remainingAfterFee = payment.amount - feeApplied;
        const orderOfApplication = feeApplied;

        if (feeApplied > 0) {
            updatePendingSummary(feeApplied);
            applyAmountToPendingRows(feeApplied);
        }

        const itemBadge = document.getElementById('selectedItemsSummary');
        if (itemBadge && itemTotal > 0) {
            itemBadge.style.display = 'inline';
            if (document.getElementById('feeItemSummary')) {
                document.getElementById('feeItemSummary').textContent = formatCurrency(itemTotal);
            }
        }

        showBanner(`Offline payment saved locally as ${payment.temp_receipt}. It will sync when online.`, 'success');
        insertOfflinePaymentHistory(payment);
        updateStudentRowsForOfflinePayment(payment, feeApplied);
    }

    function updateStudentRowsForOfflinePayment(payment, feeApplied) {
        const studentId = payment.student_id;
        const row = document.querySelector(`[data-student-id="${studentId}"]`);
        if (!row) return;
        const pendingCell = row.querySelector('.pending-amount, .total-pending, .pending-total');
        if (!pendingCell) return;
        updateElementValue(pendingCell, -feeApplied);
    }

    async function saveOfflinePaymentLocally(paymentPayload, offlineStudent) {
        try {
            const saved = await offlineStudent.savePayment(paymentPayload);
            applyOfflinePaymentToUI(saved, true);
            showToast('Payment stored offline. It will sync automatically when you are online.');
            offlineStudent.broadcastUpdate('offline_payment_created', saved);
            return saved;
        } catch (err) {
            console.error('[OfflinePayment] Save failed', err);
            showBanner('Unable to save payment offline. Please try again.', 'error');
            throw err;
        }
    }

    async function submitPaymentForm(event) {
        event.preventDefault();
        const form = getPaymentForm();
        if (!form) return;
        const paymentPayload = buildPaymentPayload(form);
        const actionUrl = form.action || window.location.href;

        if (!navigator.onLine) {
            const offlineStudent = await waitForOfflineStudent();
            return saveOfflinePaymentLocally(paymentPayload, offlineStudent);
        }

        const formData = new FormData(form);
        try {
            const response = await fetch(actionUrl, {
                method: 'POST',
                body: formData,
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRFToken': getCookie('csrftoken') || ''
                },
                credentials: 'same-origin'
            });

            if (response.ok) {
                if (response.redirected) {
                    window.location.assign(response.url);
                    return;
                }
                const text = await response.text();
                if (text.trim().length === 0) {
                    window.location.reload();
                    return;
                }
                showToast('Payment processed online. Reloading to reflect latest data.');
                window.location.reload();
                return;
            }
            throw new Error(`Server returned ${response.status}`);
        } catch (err) {
            console.warn('[OfflinePayment] Online submission failed, storing payment offline.', err);
            const offlineStudent = await waitForOfflineStudent();
            return saveOfflinePaymentLocally(paymentPayload, offlineStudent);
        }
    }

    async function syncOfflinePayments() {
        if (!navigator.onLine) return;
        let offlineStudent;
        try {
            offlineStudent = await waitForOfflineStudent();
        } catch (err) {
            console.warn('[OfflinePayment] No offlineStudent helper available', err);
            return;
        }
        const schemaName = window.AXIS_SCHEMA || getSchemaFromPath();
        if (!schemaName) {
            console.warn('[OfflinePayment] Could not determine schema for sync');
            return;
        }
        const payments = await offlineStudent.getPayments();
        if (!payments || payments.length === 0) return;

        let syncedCount = 0;
        for (const payment of payments) {
            try {
                const resp = await fetch(getPaymentEndpoint(schemaName), {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCookie('csrftoken') || ''
                    },
                    credentials: 'same-origin',
                    body: JSON.stringify(payment)
                });
                const data = await resp.json().catch(() => ({}));
                if (resp.ok && data.ok) {
                    await offlineStudent.deletePayment(payment.id);
                    replaceTempReceiptElements(payment.temp_receipt, data.receipt_number);
                    showToast(`Offline payment synced as ${data.receipt_number}`);
                    offlineStudent.broadcastUpdate('offline_payment_synced', { temp_receipt: payment.temp_receipt, receipt_number: data.receipt_number });
                    syncedCount += 1;
                    continue;
                }
                if (resp.status === 409) {
                    showBanner(data.error || 'Offline payment already processed on server.', 'success');
                    await offlineStudent.deletePayment(payment.id);
                    syncedCount += 1;
                    continue;
                }
                console.warn('[OfflinePayment] Sync rejected:', resp.status, data);
                if (data.error) {
                    showBanner(`Sync failed: ${data.error}`, 'error');
                }
            } catch (err) {
                console.warn('[OfflinePayment] Sync request failed', err);
            }
        }

        if (syncedCount > 0) {
            const currentPath = window.location.pathname;
            const shouldReload = /\/fee\/collection\//.test(currentPath) || /\/students\//.test(currentPath) || /\/defaulters\//.test(currentPath) || /\/reports\//.test(currentPath);
            if (shouldReload) {
                setTimeout(() => window.location.reload(), 1200);
            }
        }
    }

    function handleBroadcastMessage(message) {
        if (!message || !message.type) return;
        if (message.type === 'offline_payment_synced' || message.type === 'offline_payment_created') {
            if (message.type === 'offline_payment_synced') {
                replaceTempReceiptElements(message.data?.temp_receipt, message.data?.receipt_number);
            }
            setTimeout(() => {
                if (navigator.onLine) syncOfflinePayments();
            }, 500);
        }
    }

    async function restorePendingOfflinePayments() {
        let offlineStudent;
        try {
            offlineStudent = await waitForOfflineStudent();
        } catch (err) {
            return;
        }
        const payments = await offlineStudent.getPayments();
        if (!payments.length) return;
        payments.forEach(payment => {
            if (!APPLIED_PAYMENTS.has(payment.temp_receipt)) {
                applyOfflinePaymentToUI(payment, false);
            }
        });
    }

    async function init() {
        const form = getPaymentForm();
        if (form) {
            form.addEventListener('submit', submitPaymentForm);
        }
        window.addEventListener('online', syncOfflinePayments);
        window.addEventListener('focus', () => {
            if (navigator.onLine) {
                syncOfflinePayments();
            }
        });
        if ('BroadcastChannel' in window) {
            const channel = new BroadcastChannel(OFFLINE_SYNC_CHANNEL);
            channel.addEventListener('message', event => handleBroadcastMessage(event.data));
        }
        await restorePendingOfflinePayments();
        if (navigator.onLine) {
            setTimeout(syncOfflinePayments, 1500);
        }
    }

    if (document.readyState === 'complete' || document.readyState === 'interactive') {
        init().catch(err => console.error('[OfflinePayment] Init error', err));
    } else {
        document.addEventListener('DOMContentLoaded', () => init().catch(err => console.error('[OfflinePayment] Init error', err)));
    }

    window.offlinePayment = {
        submitPaymentForm,
        syncOfflinePayments,
        applyOfflinePaymentToUI,
        buildPaymentPayload
    };
})();
