// axis_saas/static/js/offline_student.js
// Offline student creation with IndexedDB sync

(function() {
    'use strict';

    const DB_NAME = 'AxisOfflineDB';
    const DB_VERSION = 2;
    const OFFLINE_STUDENTS_STORE = 'offlineStudents';
    const OFFLINE_PAYMENTS_STORE = 'offlinePayments';

    let db = null;

    function openDB() {
        return new Promise((resolve, reject) => {
            const request = indexedDB.open(DB_NAME, DB_VERSION);
            request.onupgradeneeded = (event) => {
                const database = event.target.result;
                if (!database.objectStoreNames.contains(OFFLINE_STUDENTS_STORE)) {
                    database.createObjectStore(OFFLINE_STUDENTS_STORE, { keyPath: 'id', autoIncrement: true });
                }
                if (!database.objectStoreNames.contains(OFFLINE_PAYMENTS_STORE)) {
                    const paymentsStore = database.createObjectStore(OFFLINE_PAYMENTS_STORE, { keyPath: 'id', autoIncrement: true });
                    paymentsStore.createIndex('student_id', 'student_id', { unique: false });
                    paymentsStore.createIndex('temp_receipt', 'temp_receipt', { unique: true });
                    paymentsStore.createIndex('synced', 'synced', { unique: false });
                }
            };
            request.onsuccess = (event) => resolve(event.target.result);
            request.onerror = (event) => reject(event.target.error);
        });
    }

    async function getDB() {
        if (!db) db = await openDB();
        return db;
    }

    async function saveOfflineStudent(data) {
        const database = await getDB();
        return new Promise((resolve, reject) => {
            const tx = database.transaction(OFFLINE_STUDENTS_STORE, 'readwrite');
            const store = tx.objectStore(OFFLINE_STUDENTS_STORE);
            const request = store.add(data);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async function getOfflineStudents() {
        const database = await getDB();
        return new Promise((resolve, reject) => {
            const tx = database.transaction(OFFLINE_STUDENTS_STORE, 'readonly');
            const store = tx.objectStore(OFFLINE_STUDENTS_STORE);
            const request = store.getAll();
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async function deleteOfflineStudent(id) {
        const database = await getDB();
        return new Promise((resolve, reject) => {
            const tx = database.transaction(OFFLINE_STUDENTS_STORE, 'readwrite');
            const store = tx.objectStore(OFFLINE_STUDENTS_STORE);
            const request = store.delete(id);
            request.onsuccess = () => resolve();
            request.onerror = () => reject(request.error);
        });
    }

    async function saveOfflinePayment(data) {
        const database = await getDB();
        return new Promise((resolve, reject) => {
            const tx = database.transaction(OFFLINE_PAYMENTS_STORE, 'readwrite');
            const store = tx.objectStore(OFFLINE_PAYMENTS_STORE);
            const payload = Object.assign({}, data, {
                created_at: new Date().toISOString(),
                synced: false,
                temp_receipt: data.temp_receipt || makeTempReceipt()
            });
            const request = store.add(payload);
            request.onsuccess = () => resolve(Object.assign({ id: request.result }, payload));
            request.onerror = () => reject(request.error);
        });
    }

    async function getOfflinePayments() {
        const database = await getDB();
        return new Promise((resolve, reject) => {
            const tx = database.transaction(OFFLINE_PAYMENTS_STORE, 'readonly');
            const store = tx.objectStore(OFFLINE_PAYMENTS_STORE);
            const request = store.getAll();
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async function deleteOfflinePayment(id) {
        const database = await getDB();
        return new Promise((resolve, reject) => {
            const tx = database.transaction(OFFLINE_PAYMENTS_STORE, 'readwrite');
            const store = tx.objectStore(OFFLINE_PAYMENTS_STORE);
            const request = store.delete(id);
            request.onsuccess = () => resolve();
            request.onerror = () => reject(request.error);
        });
    }

    async function getOfflinePaymentsByStudent(studentId) {
        const database = await getDB();
        return new Promise((resolve, reject) => {
            const tx = database.transaction(OFFLINE_PAYMENTS_STORE, 'readonly');
            const store = tx.objectStore(OFFLINE_PAYMENTS_STORE);
            const index = store.index('student_id');
            const request = index.getAll(Number(studentId));
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async function updateOfflinePayment(id, updates) {
        const database = await getDB();
        return new Promise((resolve, reject) => {
            const tx = database.transaction(OFFLINE_PAYMENTS_STORE, 'readwrite');
            const store = tx.objectStore(OFFLINE_PAYMENTS_STORE);
            const getReq = store.get(id);
            getReq.onsuccess = () => {
                const record = getReq.result;
                if (!record) {
                    return reject(new Error(`Offline payment ${id} not found`));
                }
                Object.assign(record, updates);
                const putReq = store.put(record);
                putReq.onsuccess = () => resolve(record);
                putReq.onerror = () => reject(putReq.error);
            };
            getReq.onerror = () => reject(getReq.error);
        });
    }

    function makeTempReceipt() {
        return `OFFLINE-${Date.now()}-${Math.random().toString(36).substr(2, 8).toUpperCase()}`;
    }

    function getCsrfToken() {
        const cookie = document.cookie.split('; ').find(row => row.trim().startsWith('csrftoken='));
        return cookie ? cookie.split('=')[1] : '';
    }

    function broadcastOfflineUpdate(type, data) {
        if (!window.BroadcastChannel) return;
        try {
            const channel = new BroadcastChannel('axis-offline-sync');
            channel.postMessage({ type, data, time: Date.now() });
        } catch (err) {
            console.warn('[Offline] Broadcast update failed', err);
        }
    }

    function receiveOfflineUpdates(callback) {
        if (!window.BroadcastChannel) return null;
        try {
            const channel = new BroadcastChannel('axis-offline-sync');
            channel.addEventListener('message', event => callback(event.data));
            return channel;
        } catch (err) {
            console.warn('[Offline] Broadcast receive failed', err);
            return null;
        }
    }

    function parseOfflineAction(form) {
        const actionUrl = form.getAttribute('action') || window.location.href;
        const editMatch = actionUrl.match(/\/students\/edit\/(\d+)\/?$/);
        return {
            action: editMatch ? 'edit' : 'create',
            student_id: editMatch ? editMatch[1] : null,
            action_url: actionUrl
        };
    }

    async function queueStudentSubmission(item) {
        return saveOfflineStudent(item);
    }

    async function submitStudentForm(form, redirectUrl = '') {
        if (!form) return false;

        const payload = Object.fromEntries(new FormData(form).entries());
        const offlineMeta = parseOfflineAction(form);
        const actionUrl = offlineMeta.action_url;

        try {
            const response = await fetch(actionUrl, {
                method: 'POST',
                body: new FormData(form),
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRFToken': getCsrfToken()
                },
                credentials: 'same-origin'
            });

            if (response.ok || response.redirected) {
                const targetUrl = response.url || redirectUrl || actionUrl;
                window.location.assign(targetUrl);
                return true;
            }

            throw new Error(`Server returned ${response.status}`);
        } catch (error) {
            if (typeof window !== 'undefined' && window.offlineStudent?.save) {
                const offlineItem = {
                    action: offlineMeta.action,
                    student_id: offlineMeta.student_id,
                    data: payload,
                    submitted_at: new Date().toISOString(),
                    redirect_to: redirectUrl,
                    action_url: actionUrl
                };
                await queueStudentSubmission(offlineItem);
                const message = offlineMeta.action === 'edit'
                    ? 'Student update saved offline. It will sync automatically when the connection returns.'
                    : 'Student saved offline. It will sync automatically when the connection returns.';
                if (window.offlineStudent?.notify) {
                    window.offlineStudent.notify(message);
                } else {
                    alert(message);
                }
                if (redirectUrl) {
                    window.location.assign(redirectUrl);
                }
                return true;
            }

            console.error('Could not save student offline', error);
            alert('Could not save student offline. Please try again.');
            return false;
        }
    }

    function isStudentListPage() {
        if (typeof window === 'undefined') return false;
        const path = window.location.pathname || '';
        return /\/portal\/[^/]+\/students\/(?:mobile\/)?$/.test(path);
    }

    function isStudentProfilePage() {
        if (typeof window === 'undefined') return false;
        const path = window.location.pathname || '';
        return /\/portal\/[^/]+\/students\/(?:mobile\/)?\d+\/?$/.test(path);
    }

    function getCurrentStudentId() {
        const path = window.location.pathname || '';
        const match = path.match(/\/students\/(?:mobile\/)?(\d+)\/?$/);
        return match ? match[1] : null;
    }

    function renderOfflineBanner(count) {
        const existing = document.querySelector('.offline-sync-banner');
        if (existing) existing.remove();
        const banner = document.createElement('div');
        banner.className = 'offline-sync-banner';
        banner.style.cssText = 'margin-bottom:1rem;padding:0.9rem 1rem;border-radius:0.75rem;background:#fef3c7;color:#92400e;font-size:0.95rem;font-weight:600;border:1px solid #fde68a;';
        banner.textContent = `You have ${count} offline student change${count === 1 ? '' : 's'} pending sync.`;
        const target = document.querySelector('.table-card, .student-list, .profile-header');
        if (target) {
            target.parentNode.insertBefore(banner, target);
        }
    }

    function buildPendingRow(item) {
        const data = item.data || {};
        const row = document.createElement('tr');
        row.dataset.offlineId = item.id;
        row.innerHTML = `
            <td><span class="roll-badge">${data.roll_number || 'TBD'}</span></td>
            <td><strong>${data.name || 'Offline Student'}</strong> <span style="display:inline-block;margin-left:0.5rem;padding:0.15rem 0.55rem;border-radius:999px;background:#fef3c7;color:#92400e;font-size:0.7rem;font-weight:700;">Pending sync</span></td>
            <td>${data.father_name || '—'}</td>
            <td>${data.grade || '—'} - ${data.section || '—'}</td>
            <td><span class="fee-pending">₹0.00</span></td>
            <td><span class="status-badge" style="background:#fde68a;color:#92400e;">Offline</span></td>
            <td class="action-btns">—</td>
        `;
        return row;
    }

    function buildPendingCard(item) {
        const data = item.data || {};
        const card = document.createElement('div');
        card.className = 'student-card offline-pending-card';
        card.dataset.offlineId = item.id;
        card.innerHTML = `
            <div class="card-top">
                <div class="student-name">${data.name || 'Offline Student'}</div>
                <span class="badge badge-offline">Pending sync</span>
            </div>
            <div class="student-meta">${data.grade || '—'}<span class="separator">•</span>${data.section || '—'}<span class="separator">•</span>Roll ${data.roll_number || 'TBD'}</div>
            <div class="student-father">${data.father_name || '—'}</div>
            <div class="student-actions"><span style="color:#92400e;font-weight:700;">Offline pending</span></div>
        `;
        return card;
    }

    async function renderPendingQueue() {
        try {
            const queue = await getOfflineStudents();
            if (!queue.length) return;
            renderOfflineBanner(queue.length);

            if (isStudentListPage()) {
                const tableBody = document.querySelector('.data-table tbody');
                if (tableBody) {
                    queue.forEach(item => {
                        const action = item.action || 'create';
                        if (action === 'create') {
                            const existing = document.querySelector(`tr[data-offline-id='${item.id}']`);
                            if (!existing) {
                                const row = buildPendingRow(item);
                                tableBody.prepend(row);
                            }
                        }
                    });
                }
                const mobileContainer = document.getElementById('studentContainer');
                if (mobileContainer) {
                    queue.forEach(item => {
                        const action = item.action || 'create';
                        if (action === 'create') {
                            const existing = document.querySelector(`.student-card[data-offline-id='${item.id}']`);
                            if (!existing) {
                                const card = buildPendingCard(item);
                                mobileContainer.prepend(card);
                            }
                        }
                    });
                }
            }

            if (isStudentProfilePage()) {
                const studentId = getCurrentStudentId();
                const pendingEdits = queue.filter(item => item.action === 'edit' && String(item.student_id) === String(studentId));
                if (pendingEdits.length) {
                    const message = pendingEdits.length === 1
                        ? 'This student has an offline edit pending sync.'
                        : `This student has ${pendingEdits.length} offline changes pending sync.`;
                    const banner = document.createElement('div');
                    banner.className = 'offline-sync-banner';
                    banner.style.cssText = 'margin-bottom:1rem;padding:0.9rem 1rem;border-radius:0.75rem;background:#fef3c7;color:#92400e;font-size:0.95rem;font-weight:600;border:1px solid #fde68a;';
                    banner.textContent = message;
                    const header = document.querySelector('.profile-header');
                    if (header) header.parentNode.insertBefore(banner, header.nextSibling);
                }
            }
        } catch (err) {
            console.error('[Offline] Error rendering pending queue:', err);
        }
    }

    function getSchemaFromPath() {
        const pathParts = window.location.pathname.split('/').filter(Boolean);
        const portalIndex = pathParts.indexOf('portal');
        return portalIndex >= 0 && pathParts.length > portalIndex + 1 ? pathParts[portalIndex + 1] : null;
    }

    function refreshStudentListPage() {
        if (!isStudentListPage()) return;
        const url = new URL(window.location.href);
        url.searchParams.set('__offline_sync', Date.now().toString());
        window.location.replace(url.toString());
    }

    async function syncOfflineStudents() {
        if (!navigator.onLine) return;
        const students = await getOfflineStudents();
        if (students.length === 0) return;

        let shouldRefreshList = false;
        const schema = window.AXIS_SCHEMA || getSchemaFromPath();
        if (!schema) {
            console.warn('No tenant schema found, cannot sync');
            return;
        }

        for (const student of students) {
            try {
                const response = await fetch(`/portal/${schema}/api/sync-offline-student/`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCsrfToken()
                    },
                    credentials: 'same-origin',
                    body: JSON.stringify(student.data)
                });
                if (response.ok) {
                    await deleteOfflineStudent(student.id);
                    shouldRefreshList = true;
                } else {
                    const errorText = await response.text();
                    console.error('Sync failed for student', student.data, errorText);
                }
            } catch (err) {
                console.error('Sync error:', err);
            }
        }

        if (shouldRefreshList) {
            setTimeout(refreshStudentListPage, 800);
        }
    }

    function showOfflineSyncBanner(message, isError = false) {
        const existing = document.querySelector('.offline-sync-notice');
        if (existing) existing.remove();
        const banner = document.createElement('div');
        banner.className = 'offline-sync-notice';
        banner.style.cssText = 'margin-bottom:1rem;padding:1rem 1.2rem;border-radius:0.75rem;border:1px solid;';
        banner.style.background = isError ? '#fee2e2' : '#ecfdf5';
        banner.style.color = isError ? '#991b1b' : '#064e3b';
        banner.style.borderColor = isError ? '#fca5a5' : '#a7f3d0';
        banner.textContent = message;
        const target = document.querySelector('.table-card, .student-list, .profile-header, .main-content, body');
        if (target) {
            target.parentNode.insertBefore(banner, target);
        } else {
            document.body.prepend(banner);
        }
    }

    function handleOfflinePaymentBroadcast(message) {
        if (!message || !message.type) return;
        if (message.type === 'offline_payment_created') {
            showOfflineSyncBanner('An offline payment is pending sync. Connect to update your data.', false);
        }
        if (message.type === 'offline_payment_synced') {
            showOfflineSyncBanner('Offline payment synced. Reloading to refresh data...', false);
            setTimeout(() => window.location.reload(), 1200);
        }
    }

    function showToast(message) {
        const existing = document.querySelector('.offline-toast');
        if (existing) existing.remove();
        const toast = document.createElement('div');
        toast.className = 'offline-toast';
        toast.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#10b981;color:#fff;padding:0.85rem 1rem;border-radius:999px;box-shadow:0 10px 24px rgba(0,0,0,0.18);z-index:9999;font-weight:700;';
        toast.textContent = message;
        document.body.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transition = 'opacity 0.25s ease';
            setTimeout(() => toast.remove(), 250);
        }, 4000);
    }

    window.offlineStudent = {
        save: saveOfflineStudent,
        delete: deleteOfflineStudent,
        getPending: getOfflineStudents,
        savePayment: saveOfflinePayment,
        deletePayment: deleteOfflinePayment,
        getPayments: getOfflinePayments,
        getPaymentsByStudent: getOfflinePaymentsByStudent,
        updatePayment: updateOfflinePayment,
        sync: syncOfflineStudents,
        notify: showToast,
        queue: queueStudentSubmission,
        submitForm: submitStudentForm,
        broadcastUpdate: broadcastOfflineUpdate,
        receiveUpdates: receiveOfflineUpdates,
        getCsrfToken: getCsrfToken
    };

    window.addEventListener('online', syncOfflineStudents);
    document.addEventListener('DOMContentLoaded', () => {
        renderPendingQueue();
        if ('BroadcastChannel' in window) {
            const channel = new BroadcastChannel('axis-offline-sync');
            channel.addEventListener('message', event => {
                const message = event.data;
                if (!message || !message.type) return;
                if (message.type === 'offline_payment_created') {
                    showOfflineSyncBanner('An offline payment is pending sync. Connect to update your data.');
                }
                if (message.type === 'offline_payment_synced') {
                    showOfflineSyncBanner('Offline payment synced. Reloading to refresh data...');
                    setTimeout(() => window.location.reload(), 1200);
                }
            });
        }
        if (navigator.onLine) {
            setTimeout(syncOfflineStudents, 3000);
        }
    });
})();
