// ============================================================================
// InfinityChat 2.0 — client
// Conversations, private chats (up to 3 per person), instant read receipts,
// inline image embeds (<img> tags / markdown), reliable edit/delete.
// ============================================================================
(function () {
    'use strict';

    const APP_VERSION = '2.0.0';

    // ============================================================
    // Safe storage
    // Storage access can throw in sandboxed iframes / private mode /
    // when cookies are blocked - that must never kill the whole app.
    // Falls back to an in-memory store for the lifetime of the tab.
    // ============================================================
    const memStore = new Map();
    const store = {
        get(k) {
            try { return localStorage.getItem(k); } catch (e) { return memStore.has(k) ? memStore.get(k) : null; }
        },
        set(k, v) {
            try { localStorage.setItem(k, v); } catch (e) { memStore.set(k, v); }
        },
        remove(k) {
            try { localStorage.removeItem(k); } catch (e) { memStore.delete(k); }
        }
    };

    // ============================================================
    // State
    // ============================================================
    const State = {
        token: store.get('token'),
        user: null,                       // {id, username, display_name, avatar_path}
        ws: null,
        wsState: 'disconnected',
        reconnectAttempt: 0,
        lastPong: Date.now(),

        conversations: new Map(),         // convId -> summary object
        conversationOrder: [],            // convIds sorted for the sidebar

        activeConvId: null,               // conversation currently on screen
        convCache: new Map(),             // convId -> {ids:[], cursor, hasMore, lastTs}
        messages: new Map(),              // messageId -> message object
        tombstoned: new Map(),            // convId -> Set(messageId) shown as "deleted"

        pendingSends: new Map(),          // client_id -> payload (for resend after reconnect)
        replyTo: null,                    // message being replied to
        editingId: null,                  // message id currently being edited inline
        pendingFile: null,                // uploaded file metadata waiting for send
        typingUsers: new Map(),           // convId -> {userId -> {name, timer}}
        typingSentAt: 0,
        onlineUsers: new Map(),
        isAtBottom: true,
        latestUnseen: null,
        unreadTotal: 0,

        theme: store.get('theme') || 'light',
        customColors: {
            self: store.get('custom-self') || '#2563EB',
            other: store.get('custom-other') || '#F3F4F6',
            text: store.get('custom-text') || '#111827'
        },
        prefs: {
            autoScroll: store.get('pref-autoscroll') !== 'false',
            showScrollBtn: store.get('pref-scrollbtn') !== 'false',
            showLatestBar: store.get('pref-latestbar') !== 'false',
            sound: store.get('pref-sound') !== 'false',
            enterSend: store.get('pref-entersend') !== 'false'
        }
    };

    // ============================================================
    // DOM cache
    // ============================================================
    const $ = (id) => document.getElementById(id);
    const DOM = {
        authScreen: $('auth-screen'), chatScreen: $('chat-screen'),
        loginForm: $('login-form'), signupForm: $('signup-form'),
        authTabs: document.querySelectorAll('.auth-tab'),
        authError: $('auth-error'), authErrorText: $('auth-error-text'),
        authLoading: $('auth-loading'),

        sidebar: $('sidebar'), sidebarBackdrop: $('sidebar-backdrop'),
        sidebarOpenBtn: $('sidebar-open-btn'), sidebarCloseBtn: $('sidebar-close-btn'),
        sbMeName: $('sb-me-name'), sbMeAvatar: $('sb-me-avatar'), sbMeAvatarPh: $('sb-me-avatar-ph'),
        convList: $('conv-list'),

        convTitle: $('conv-title'), convSubtitle: $('conv-subtitle'),
        convAvatar: $('conv-avatar'), convAvatarPh: $('conv-avatar-ph'),
        convPresenceDot: $('conv-presence-dot'),
        onlineBadge: $('online-badge'),

        connectionStatus: $('connection-status'), connectionText: $('connection-text'),
        messagesList: $('messages-list'), messagesScroll: $('messages-scroll'),
        emptyState: $('empty-state'), emptyIcon: $('empty-icon'),
        emptyTitle: $('empty-title'), emptySub: $('empty-sub'),
        loadOlderBtn: $('load-older-btn'),
        scrollBottomBtn: $('scroll-bottom-btn'),
        latestMessageBar: $('latest-message-bar'),
        lmbAvatar: $('lmb-avatar'), lmbName: $('lmb-name'), lmbText: $('lmb-text'),
        typingIndicator: $('typing-indicator'), typingText: $('typing-text'),
        replyPreview: $('reply-preview'), replyUser: $('reply-user'), replyText: $('reply-text'),
        composeForm: $('compose-form'), messageInput: $('message-input'), sendBtn: $('send-btn'),
        attachBtn: $('attach-btn'), emojiBtn: $('emoji-btn'), fileInput: $('file-input'),
        uploadPreview: $('upload-preview'), uploadIcon: $('upload-icon'), uploadFilename: $('upload-filename'),
        uploadSize: $('upload-size'), cancelUpload: $('cancel-upload'),
        uploadProgress: $('upload-progress'), progressFill: $('progress-fill'), progressText: $('progress-text'),
        dropOverlay: $('drop-overlay'),

        settingsModal: $('settings-modal'), onlineModal: $('online-modal'),
        convModal: $('conv-modal'), receiptsModal: $('receipts-modal'),
        confirmModal: $('confirm-modal'), lightboxModal: $('lightbox-modal'),
        emojiPicker: $('emoji-picker'), emojiGrid: $('emoji-grid'),

        settingsDisplayName: $('settings-display-name'), settingsUsername: $('settings-username'),
        settingsAvatarPreview: $('settings-avatar-preview'), settingsAvatarImg: $('settings-avatar-img'),
        avatarInput: $('avatar-input'), avatarUploadBtn: $('avatar-upload-btn'),
        themeOptions: document.querySelectorAll('.theme-option'),
        customThemeSettings: $('custom-theme-settings'),
        customSelfColor: $('custom-self-color'), customOtherColor: $('custom-other-color'),
        customTextColor: $('custom-text-color'),
        selfColorHex: $('self-color-hex'), otherColorHex: $('other-color-hex'), textColorHex: $('text-color-hex'),
        toggleAutoScroll: $('toggle-autoscroll'), toggleScrollBtn: $('toggle-scroll-btn'),
        toggleLatestBar: $('toggle-latest-bar'), toggleSound: $('toggle-sound'),
        toggleEnterSend: $('toggle-enter-send'), notifyPermBtn: $('notify-perm-btn'),
        pwCurrent: $('pw-current'), pwNew: $('pw-new'), pwChangeBtn: $('pw-change-btn'), pwMsg: $('pw-msg'),
        onlineBtn: $('online-btn'), onlineUsersList: $('online-users-list'), onlineTotal: $('online-total'),
        settingsBtn: $('settings-btn'), logoutBtn: $('logout-btn'), meSettingsBtn: $('me-settings-btn'),
        sidebarLogoutBtn: $('sidebar-logout-btn'),
        convModalDesc: $('conv-modal-desc'), convModalList: $('conv-modal-list'), newChatBtn: $('new-chat-btn'),
        receiptsSummary: $('receipts-summary'), receiptsList: $('receipts-list'),
        confirmTitle: $('confirm-title'), confirmMessage: $('confirm-message'),
        confirmOkBtn: $('confirm-ok-btn'), confirmCancelBtn: $('confirm-cancel-btn'),
        lightboxImg: $('lightbox-img'), lightboxName: $('lightbox-name'), lightboxDownload: $('lightbox-download'),
        toastContainer: $('toast-container'), aboutVersion: $('about-version')
    };

    const API = window.location.origin;
    const GLOBAL_CONV = 1;

    // ============================================================
    // Small helpers
    // ============================================================
    function generateId() {
        return Date.now().toString(36) + Math.random().toString(36).substr(2, 9);
    }

    function truncate(str, n) {
        if (!str) return '';
        return str.length > n ? str.substr(0, n - 1) + '…' : str;
    }

    function escapeHtml(text) {
        if (text === null || text === undefined) return '';
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }

    function fileUrl(filePath) {
        return `${API}/api/download/${encodeURIComponent(filePath)}?t=${encodeURIComponent(State.token)}`;
    }

    function displayNameOf(u) {
        if (!u) return 'Unknown';
        // user-shaped objects (peers, online users, receipts)...
        if (u.display_name || u.username) return u.display_name || u.username;
        // ...or message-shaped objects (sender_display_name / sender_username)
        if (u.sender_display_name || u.sender_username) return u.sender_display_name || u.sender_username;
        return 'Unknown';
    }

    function avatarColorClass(seed) {
        let h = 0;
        const s = String(seed || '');
        for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
        return 'avatar-c' + (h % 6);
    }

    function initialOf(name) {
        return (name || '?').charAt(0).toUpperCase();
    }

    // ---------- time ----------
    function formatTime(ms) {
        if (!ms) return '';
        const date = new Date(ms);
        const now = new Date();
        const diff = now - date;
        if (diff < 45000) return 'now';
        if (diff < 3600000) return Math.max(1, Math.floor(diff / 60000)) + 'm';
        if (date.toDateString() === now.toDateString()) {
            return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        const yesterday = new Date(now);
        yesterday.setDate(yesterday.getDate() - 1);
        if (date.toDateString() === yesterday.toDateString()) {
            return 'Yesterday ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        if (diff < 6 * 86400000) {
            return date.toLocaleDateString([], { weekday: 'short' }) + ' ' +
                date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        return date.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
            date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function formatDayLabel(ms) {
        const d = new Date(ms), now = new Date();
        if (d.toDateString() === now.toDateString()) return 'Today';
        const y = new Date(now);
        y.setDate(y.getDate() - 1);
        if (d.toDateString() === y.toDateString()) return 'Yesterday';
        if (d.getFullYear() === now.getFullYear()) {
            return d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
        }
        return d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
    }

    function formatLastSeen(sec) {
        if (!sec) return 'offline';
        const diff = Date.now() / 1000 - sec;
        if (diff < 60) return 'online';
        if (diff < 3600) return 'last seen ' + Math.floor(diff / 60) + 'm ago';
        if (diff < 86400) return 'last seen ' + Math.floor(diff / 3600) + 'h ago';
        return 'last seen ' + Math.floor(diff / 86400) + 'd ago';
    }

    function formatFileSize(bytes) {
        if (!bytes) return '0 B';
        const k = 1024, sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.min(sizes.length - 1, Math.floor(Math.log(bytes) / Math.log(k)));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }

    function dayKey(ms) {
        return new Date(ms).toDateString();
    }

    // ---------- toast ----------
    function showToast(message, type = 'info', ms = 4000) {
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        const icons = { success: 'check-circle', error: 'exclamation-circle', warning: 'exclamation-triangle', info: 'info-circle' };
        toast.innerHTML = `<i class="fas fa-${icons[type] || 'info-circle'}"></i><span>${escapeHtml(message)}</span>`;
        DOM.toastContainer.appendChild(toast);
        setTimeout(() => toast.remove(), ms);
    }

    // ---------- modals ----------
    const openModals = [];

    function openModal(el) {
        if (!el) return;
        el.classList.remove('hidden');
        if (!openModals.includes(el)) openModals.push(el);
        document.body.style.overflow = 'hidden';
    }

    function closeModal(el) {
        if (!el) return;
        el.classList.add('hidden');
        const i = openModals.indexOf(el);
        if (i > -1) openModals.splice(i, 1);
        if (!openModals.length) document.body.style.overflow = '';
        // resolve any pending confirm() promise (backdrop / X / Escape path)
        if (el === DOM.confirmModal && el.__resolve) {
            const r = el.__resolve;
            el.__resolve = null;
            r(false);
        }
    }

    function closeTopModal() {
        const el = openModals[openModals.length - 1];
        if (el) closeModal(el);
    }

    // generic backdrop + [data-close] delegation
    document.addEventListener('click', (e) => {
        const closer = e.target.closest('[data-close]');
        if (closer) {
            const id = closer.dataset.close;
            closeModal(document.getElementById(id));
            if (id === 'settings-modal') applyPrefs();
        }
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && openModals.length) {
            if (State.editingId) { cancelInlineEdit(); return; }
            closeTopModal();
        }
    });

    // ---------- custom confirm (replaces blocking confirm()) ----------
    function showConfirm({ title = 'Are you sure?', message = '', okText = 'OK', danger = true }) {
        return new Promise((resolve) => {
            DOM.confirmTitle.innerHTML = `<i class="fas fa-${danger ? 'exclamation-triangle' : 'question-circle'}"></i> ${escapeHtml(title)}`;
            DOM.confirmMessage.textContent = message;
            DOM.confirmOkBtn.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;
            DOM.confirmOkBtn.innerHTML = `<i class="fas fa-${danger ? 'trash' : 'check'}"></i> ${escapeHtml(okText)}`;

            const done = (val) => {
                DOM.confirmOkBtn.removeEventListener('click', onOk);
                DOM.confirmCancelBtn.removeEventListener('click', onCancel);
                DOM.confirmModal.__resolve = null;
                closeModal(DOM.confirmModal);
                resolve(val);
            };
            const onOk = () => done(true);
            const onCancel = () => done(false);
            DOM.confirmOkBtn.addEventListener('click', onOk);
            DOM.confirmCancelBtn.addEventListener('click', onCancel);
            // backdrop / X button / Escape resolve as "cancel" via closeModal hook
            DOM.confirmModal.__resolve = (val) => {
                DOM.confirmOkBtn.removeEventListener('click', onOk);
                DOM.confirmCancelBtn.removeEventListener('click', onCancel);
                resolve(val);
            };
            openModal(DOM.confirmModal);
        });
    }

    // ============================================================
    // Theme & preferences
    // ============================================================
    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        State.theme = theme;
        store.set('theme', theme);
        if (theme === 'custom') {
            const s = State.customColors.self, o = State.customColors.other, t = State.customColors.text;
            document.documentElement.style.setProperty('--bubble-self', s);
            document.documentElement.style.setProperty('--bubble-other', o);
            document.documentElement.style.setProperty('--bubble-other-text', t);
            const isLight = (c) => {
                const r = parseInt(c.slice(1, 3), 16), g = parseInt(c.slice(3, 5), 16), b = parseInt(c.slice(5, 7), 16);
                return (0.299 * r + 0.587 * g + 0.114 * b) > 150;
            };
            document.documentElement.style.setProperty('--bubble-self-text', isLight(s) ? '#111827' : '#FFFFFF');
        } else {
            ['--bubble-self', '--bubble-other', '--bubble-other-text', '--bubble-self-text']
                .forEach(p => document.documentElement.style.removeProperty(p));
        }
    }
    applyTheme(State.theme);

    function applyPrefs() {
        DOM.toggleAutoScroll.checked = State.prefs.autoScroll;
        DOM.toggleScrollBtn.checked = State.prefs.showScrollBtn;
        DOM.toggleLatestBar.checked = State.prefs.showLatestBar;
        DOM.toggleSound.checked = State.prefs.sound;
        DOM.toggleEnterSend.checked = State.prefs.enterSend;
        DOM.notifyPermBtn.innerHTML = (window.Notification && Notification.permission === 'granted')
            ? '<i class="fas fa-check"></i> Enabled'
            : '<i class="fas fa-bell"></i> Enable';
        updateScrollUI();
    }

    function savePrefs() {
        store.set('pref-autoscroll', State.prefs.autoScroll);
        store.set('pref-scrollbtn', State.prefs.showScrollBtn);
        store.set('pref-latestbar', State.prefs.showLatestBar);
        store.set('pref-sound', State.prefs.sound);
        store.set('pref-entersend', State.prefs.enterSend);
    }

    [['toggle-autoscroll', 'autoScroll'], ['toggle-scroll-btn', 'showScrollBtn'],
     ['toggle-latest-bar', 'showLatestBar'], ['toggle-sound', 'sound'],
     ['toggle-enter-send', 'enterSend']].forEach(([id, key]) => {
        const el = document.getElementById(id);
        el.addEventListener('change', () => {
            State.prefs[key] = el.checked;
            savePrefs();
            if (key === 'autoScroll' && State.prefs.autoScroll) { scrollToBottom(); hideLatestBar(); }
            if (key === 'showScrollBtn') updateScrollUI();
            if (key === 'showLatestBar' && !State.prefs.showLatestBar) hideLatestBar();
        });
    });

    DOM.notifyPermBtn.addEventListener('click', async () => {
        if (!('Notification' in window)) { showToast('Notifications not supported here', 'warning'); return; }
        try {
            const perm = await Notification.requestPermission();
            DOM.notifyPermBtn.innerHTML = perm === 'granted'
                ? '<i class="fas fa-check"></i> Enabled'
                : '<i class="fas fa-bell-slash"></i> Blocked';
            if (perm === 'granted') showToast('Desktop notifications enabled', 'success');
        } catch (e) { /* ignore */ }
    });

    // notification sound (WebAudio - no asset needed)
    let audioCtx = null;
    function playChime() {
        if (!State.prefs.sound) return;
        try {
            audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
            if (audioCtx.state === 'suspended') audioCtx.resume();
            const now = audioCtx.currentTime;
            const notes = [880, 1174.66];
            notes.forEach((freq, i) => {
                const osc = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                osc.type = 'sine';
                osc.frequency.value = freq;
                const t0 = now + i * 0.09;
                gain.gain.setValueAtTime(0.0001, t0);
                gain.gain.exponentialRampToValueAtTime(0.12, t0 + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.16);
                osc.connect(gain).connect(audioCtx.destination);
                osc.start(t0); osc.stop(t0 + 0.2);
            });
        } catch (e) { /* audio unavailable */ }
    }

    // ============================================================
    // Auth
    // ============================================================
    DOM.authTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const target = tab.dataset.tab;
            DOM.authTabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            DOM.loginForm.classList.toggle('active', target === 'login');
            DOM.signupForm.classList.toggle('active', target === 'signup');
            DOM.authError.style.display = 'none';
        });
    });

    DOM.loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        await login($('login-username').value.trim(), $('login-password').value);
    });
    DOM.signupForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const username = $('signup-username').value.trim();
        const display = $('signup-display').value.trim() || username;
        const password = $('signup-password').value;
        await signup(username, password, display);
    });

    async function postJson(url) {
        const res = await fetch(url, { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
        return data;
    }

    async function login(username, password) {
        DOM.authLoading.classList.remove('hidden');
        hideAuthError();
        try {
            const data = await postJson(
                `${API}/api/auth/login?username=${encodeURIComponent(username)}&password=${encodeURIComponent(password)}`);
            handleAuthSuccess(data);
        } catch (err) { showAuthError(err.message); }
        finally { DOM.authLoading.classList.add('hidden'); }
    }

    async function signup(username, password, display) {
        DOM.authLoading.classList.remove('hidden');
        hideAuthError();
        try {
            const data = await postJson(
                `${API}/api/auth/signup?username=${encodeURIComponent(username)}&password=${encodeURIComponent(password)}&display_name=${encodeURIComponent(display)}`);
            handleAuthSuccess(data);
        } catch (err) { showAuthError(err.message); }
        finally { DOM.authLoading.classList.add('hidden'); }
    }

    function handleAuthSuccess(data) {
        State.token = data.token;
        State.user = data.user;
        store.set('token', State.token);
        showChat();
        connectWebSocket();
    }

    function showAuthError(msg) {
        DOM.authErrorText.textContent = msg;
        DOM.authError.style.display = 'flex';
    }
    function hideAuthError() { DOM.authError.style.display = 'none'; }

    // ============================================================
    // WebSocket (heartbeat + resilient reconnect + pending resend)
    // ============================================================
    function connectWebSocket() {
        if (!State.token) return;
        if (State.ws && (State.ws.readyState === WebSocket.OPEN || State.ws.readyState === WebSocket.CONNECTING)) return;
        State.wsState = 'connecting';
        updateConnectionUI();
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(`${proto}//${location.host}/ws?token=${encodeURIComponent(State.token)}`);
        State.ws = ws;

        ws.onopen = () => {
            State.wsState = 'connected';
            State.reconnectAttempt = 0;
            updateConnectionUI();
            requestConversations();
            flushPendingSends();
        };
        ws.onmessage = (e) => {
            try { handleWSMessage(JSON.parse(e.data)); } catch (err) { console.error('WS parse error', err); }
        };
        ws.onclose = (ev) => {
            if (ev.code === 4001) {
                // token invalid/revoked -> log out
                forceLogout();
                return;
            }
            if (State.wsState === 'connected' || State.wsState === 'connecting') {
                scheduleReconnect();
            }
        };
        ws.onerror = () => {
            State.wsState = 'disconnected';
            updateConnectionUI();
        };
    }

    function scheduleReconnect() {
        State.wsState = 'reconnecting';
        updateConnectionUI();
        State.reconnectAttempt += 1;
        const base = Math.min(15000, 800 * Math.pow(1.7, State.reconnectAttempt - 1));
        const delay = base * (0.6 + Math.random() * 0.8);
        setTimeout(() => connectWebSocket(), delay);
    }

    // keepalive so proxies never kill an idle socket
    setInterval(() => {
        if (State.ws && State.ws.readyState === WebSocket.OPEN) {
            State.ws.send(JSON.stringify({ type: 'ping', t: Date.now() }));
        }
    }, 20000);

    function wsOpen() {
        return State.ws && State.ws.readyState === WebSocket.OPEN;
    }

    function wsSend(obj) {
        if (!wsOpen()) return false;
        try { State.ws.send(JSON.stringify(obj)); return true; } catch (e) { return false; }
    }

    function updateConnectionUI() {
        const map = {
            connected: ['connected', 'Connected'],
            reconnecting: ['reconnecting', 'Reconnecting…'],
            disconnected: ['disconnected', 'Disconnected'],
            connecting: ['reconnecting', 'Connecting…']
        };
        const [cls, txt] = map[State.wsState] || ['disconnected', 'Disconnected'];
        DOM.connectionStatus.className = 'connection-status ' + cls;
        DOM.connectionText.textContent = txt;
    }

    // ============================================================
    // Conversations / sidebar
    // ============================================================
    function convById(id) { return State.conversations.get(id); }

    function upsertConversation(summary) {
        State.conversations.set(summary.id, summary);
        if (!State.convCache.has(summary.id)) {
            State.convCache.set(summary.id, { ids: [], cursor: null, hasMore: true, lastTs: 0 });
        }
        renderSidebar();
    }

    function deleteConversationLocally(id) {
        // reserved: DM deletion UI may land in a later version
    }

    function convSortKey(c) {
        if (c.id === GLOBAL_CONV) return [0, 0];
        return [1, -(c.last_message_ts || 0)];
    }

    function renderSidebar() {
        State.conversationOrder = [...State.conversations.values()].sort((a, b) => {
            const ka = convSortKey(a), kb = convSortKey(b);
            return ka[0] - kb[0] || ka[1] - kb[1];
        });

        let html = '';
        let prevType = null;
        let unreadTotal = 0;
        State.conversationOrder.forEach((c) => {
            if (c.type === 'dm' && prevType !== 'dm') {
                html += '<div class="conv-section-label">Private Chats</div>';
            }
            prevType = c.type;
            unreadTotal += c.unread_count || 0;
            html += convItemHTML(c);
        });
        if (!State.conversationOrder.length) {
            html = '<div class="conv-empty">Loading chats…</div>';
        }
        DOM.convList.innerHTML = html;
        DOM.convList.querySelectorAll('.conv-item').forEach((el) => {
            el.addEventListener('click', () => {
                switchConversation(parseInt(el.dataset.id, 10));
                closeSidebar();
            });
        });
        State.unreadTotal = unreadTotal;
        updateTitleUnread();
    }

    function updateTitleUnread() {
        const label = State.unreadTotal > 0 ? `(${State.unreadTotal > 99 ? '99+' : State.unreadTotal}) ` : '';
        document.title = label + 'InfinityChat';
    }

    function convItemHTML(c) {
        const isDm = c.type === 'dm';
        const name = isDm ? dmLabel(c) : (c.title || 'Global Chat');
        const active = c.id === State.activeConvId ? ' active' : '';
        const time = c.last_message_ts ? formatTime(c.last_message_ts) : '';
        const unread = Math.min(c.unread_count || 0, 99);
        const unreadHtml = unread > 0
            ? `<span class="conv-badge">${unread}</span>`
            : '';
        const timeCls = unread > 0 ? ' unread' : '';
        let avatar = avatarHTML(c, 44);
        return `<button class="conv-item${active}" data-id="${c.id}" title="${escapeHtml(name)}">
            ${avatar}
            <div class="conv-info">
                <div class="conv-top">
                    <span class="conv-name">${escapeHtml(name)}</span>
                    <span class="conv-time${timeCls}">${escapeHtml(time)}</span>
                </div>
                <div class="conv-bottom">
                    <span class="conv-preview">${previewHTML(c)}</span>
                    ${unreadHtml}
                </div>
            </div>
        </button>`;
    }

    function dmLabel(c) {
        const base = displayNameOf(c.peer);
        return c.dm_number && c.dm_number > 1 ? `${base} · ${c.dm_number}` : base;
    }

    function previewHTML(c) {
        let txt = c.last_message_preview || (c.id === GLOBAL_CONV ? 'No messages yet' : 'No messages yet');
        if (c.last_sender_id && !(c.last_sender_id === State.user?.id)) {
            const n = c.last_sender_name;
            if (c.type === 'global') txt = `${n}: ${txt}`;
        }
        return escapeHtml(truncate(txt, 60));
    }

    function avatarHTML(c, px) {
        if (c.type === 'dm' && c.peer) {
            const on = c.peer.online ? ' online' : '';
            let inner = `<span class="conv-presence${on}"></span>`;
            if (c.peer.avatar_path) {
                inner = `<img src="${fileUrl(c.peer.avatar_path)}" alt="" loading="lazy">${inner}`;
            } else {
                inner = `${initialOf(displayNameOf(c.peer))}${inner}`;
            }
            return `<div class="conv-avatar ${avatarColorClass(c.peer.username || c.peer.id)}" style="width:${px}px;height:${px}px;font-size:${Math.round(px * 0.4)}px">${inner}</div>`;
        }
        return `<div class="conv-avatar avatar-c0" style="width:${px}px;height:${px}px"><i class="fas fa-globe"></i></div>`;
    }

    function requestConversations() {
        if (wsOpen()) wsSend({ type: 'request_conversations' });
    }

    function getConv(convId) {
        if (!State.convCache.has(convId)) {
            State.convCache.set(convId, { ids: [], cursor: null, hasMore: true, lastTs: 0 });
        }
        return State.convCache.get(convId);
    }

    function loadMessages(convId = State.activeConvId, opts = {}) {
        if (!wsOpen()) return;
        const cache = getConv(convId);
        const msg = { type: 'load_messages', conversation_id: convId, limit: 50 };
        if (opts.older && cache.cursor) msg.cursor = cache.cursor;
        wsSend(msg);
    }

    // ============================================================
    // Conversation switching
    // ============================================================
    function switchConversation(convId) {
        convId = parseInt(convId, 10) || GLOBAL_CONV;
        if (convId === State.activeConvId) return;
        if (State.editingId) cancelInlineEdit();
        clearReply();              // replies are conversation-scoped
        hideLatestBar();
        State.activeConvId = convId;
        State.latestUnseen = null;
        State.isAtBottom = true;

        const cache = getConv(convId);
        renderConversationHeader(convId);
        renderSidebar();

        if (cache.ids.length === 0) {
            cache.loading = true;
            loadMessages(convId);
            renderEmptyLoading(convId);
        } else {
            renderAllMessages(convId, { scrollBottom: true });
        }
        markActiveRead(true);
        DOM.messageInput.focus({ preventScroll: true });
    }

    function renderEmptyLoading(convId) {
        DOM.messagesList.innerHTML = '';
        // re-attach the empty-state element (innerHTML wipes it) before showing it
        if (DOM.emptyState.parentNode !== DOM.messagesList) DOM.messagesList.appendChild(DOM.emptyState);
        const conv = convById(convId);
        const dm = conv && conv.type === 'dm' && conv.peer;
        if (dm) {
            DOM.emptyIcon.innerHTML = '<i class="fas fa-user-lock"></i>';
            DOM.emptyTitle.textContent = 'No messages yet';
            DOM.emptySub.textContent = `This is the start of your private chat with ${displayNameOf(conv.peer)}`;
        } else {
            DOM.emptyIcon.innerHTML = '<i class="fas fa-comments"></i>';
            DOM.emptyTitle.textContent = 'No messages yet';
            DOM.emptySub.textContent = 'Say hello to the Global Chat below';
        }
        DOM.emptyState.style.display = 'flex';
        DOM.loadOlderBtn.classList.add('hidden');
    }

    function renderConversationHeader(convId) {
        const conv = convById(convId);
        if (!conv) return;
        const dm = conv.type === 'dm';
        const peer = conv.peer;

        if (dm && peer) {
            DOM.convTitle.textContent = dmLabel(conv);
            if (peer.avatar_path) {
                DOM.convAvatar.src = fileUrl(peer.avatar_path);
                DOM.convAvatar.classList.remove('hidden');
                DOM.convAvatarPh.classList.add('hidden');
            } else {
                DOM.convAvatar.classList.add('hidden');
                DOM.convAvatarPh.classList.remove('hidden');
                DOM.convAvatarPh.className = 'conv-avatar-ph ' + avatarColorClass(peer.username || peer.id);
                DOM.convAvatarPh.textContent = initialOf(displayNameOf(peer));
            }
            const online = peer.online || State.onlineUsers.has(peer.id);
            DOM.convPresenceDot.classList.remove('hidden');
            DOM.convPresenceDot.classList.toggle('offline', !online);
            DOM.convSubtitle.innerHTML = online
                ? '<span class="status-dot online"></span> Online'
                : escapeHtml(formatLastSeen(peer.last_seen));
            DOM.onlineBtn.style.display = '';
        } else {
            DOM.convTitle.textContent = 'Global Chat';
            DOM.convAvatar.classList.add('hidden');
            DOM.convAvatarPh.classList.remove('hidden');
            DOM.convAvatarPh.className = 'conv-avatar-ph';
            DOM.convAvatarPh.innerHTML = '<i class="fas fa-globe"></i>';
            DOM.convPresenceDot.classList.add('hidden');
            const meCount = State.onlineUsers.has(State.user?.id) ? State.onlineUsers.size - 1 : State.onlineUsers.size;
            DOM.convSubtitle.innerHTML = `<span class="status-dot online"></span> ${Math.max(0, meCount)} online`;
            DOM.onlineBtn.style.display = '';
        }
    }

    function openSidebar() {
        DOM.sidebar.classList.add('open');
        DOM.sidebarBackdrop.classList.remove('hidden');
    }
    function closeSidebar() {
        DOM.sidebar.classList.remove('open');
        DOM.sidebarBackdrop.classList.add('hidden');
    }
    DOM.sidebarOpenBtn.addEventListener('click', openSidebar);
    DOM.sidebarCloseBtn.addEventListener('click', closeSidebar);
    DOM.sidebarBackdrop.addEventListener('click', closeSidebar);

    // ============================================================
    // Read receipts
    // ============================================================
    function latestMessageId(convId) {
        const ids = getConv(convId).ids;
        return ids.length ? ids[ids.length - 1] : null;
    }

    // coalesced, non-blocking "I have this chat open right now"
    let markReadTimer = null;
    function markActiveRead(force) {
        if (!wsOpen()) return;
        const convId = State.activeConvId;
        if (document.visibilityState !== 'visible' && !force) return;
        const upTo = latestMessageId(convId);
        if (!upTo) return;
        clearTimeout(markReadTimer);
        markReadTimer = setTimeout(() => {
            const summary = convById(convId);
            if (summary) {
                // optimistically zero the badge; server confirms via conversation_updated
                if (summary.unread_count) { summary.unread_count = 0; renderSidebar(); }
            }
            wsSend({ type: 'mark_read', up_to_message_id: upTo, conversation_id: convId });
        }, 120);
    }

    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
            markActiveRead(true);
            if (State.activeConvId) renderConversationHeader(State.activeConvId);
        } else {
            updateTitleUnread();
        }
    });

    // clicking the tick = who read it, and when
    function openReceipts(messageId) {
        const msg = State.messages.get(messageId);
        if (!msg) return;
        DOM.receiptsList.innerHTML = '<div class="loading-state"><div class="spinner spinner-sm"></div><span>Loading…</span></div>';
        DOM.receiptsSummary.textContent = '';
        openModal(DOM.receiptsModal);
        fetch(`${API}/api/messages/${messageId}/read-receipts`, { headers: { 'X-Auth-Token': State.token } })
            .then(async (res) => {
                if (!res.ok) {
                    const data = await res.json().catch(() => ({}));
                    throw new Error(data.detail || 'Failed');
                }
                return res.json();
            })
            .then((data) => renderReceipts(data, messageId))
            .catch((err) => {
                DOM.receiptsList.innerHTML = `<div class="receipts-empty"><i class="fas fa-exclamation-circle"></i> ${escapeHtml(err.message)}</div>`;
            });
    }

    function renderReceipts(data) {
        const n = data.reader_count || 0;
        if (data.is_dm && !data.not_read.length) {
            DOM.receiptsSummary.innerHTML = `<i class="fas fa-check-double" style="color:var(--success)"></i> Read`;
        } else if (!data.is_dm && n > 0) {
            DOM.receiptsSummary.textContent = `Read by ${n} ${n === 1 ? 'user' : 'users'}`;
        } else if (data.not_read.length) {
            DOM.receiptsSummary.textContent = 'Not read yet';
        } else {
            DOM.receiptsSummary.textContent = `No one has read this yet`;
        }

        let html = '';
        if (!data.readers.length) {
            html = `<div class="receipts-empty">No read receipts yet${data.is_dm && data.not_read.length ? ` — ${displayNameOf(data.not_read[0])} hasn't opened this chat since you sent it` : ''}</div>`;
        }
        data.readers.forEach(r => {
            const av = r.avatar_path
                ? `<img src="${fileUrl(r.avatar_path)}" alt="" style="width:100%;height:100%;object-fit:cover">`
                : `<span>${initialOf(displayNameOf(r))}</span>`;
            html += `<div class="receipt-row">
                <div class="user-avatar">${av}</div>
                <div class="ri-info">
                    <div class="ri-name">${escapeHtml(displayNameOf(r))}${r.online ? ' <span class="status-dot online" style="vertical-align:middle"></span>' : ''}</div>
                    <div class="ri-time">Read ${new Date(r.read_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</div>
                </div>
                <i class="fas fa-check-double ri-flag"></i>
            </div>`;
        });
        // In a DM, explicitly show who hasn't read yet
        data.not_read.forEach(r => {
            const av = r.avatar_path
                ? `<img src="${fileUrl(r.avatar_path)}" alt="" style="width:100%;height:100%;object-fit:cover">`
                : `<span>${initialOf(displayNameOf(r))}</span>`;
            html += `<div class="receipt-row unread-row">
                <div class="user-avatar">${av}</div>
                <div class="ri-info">
                    <div class="ri-name">${escapeHtml(displayNameOf(r))}</div>
                    <div class="ri-time">${r.online ? 'Online — has not read it yet' : 'Not read yet'}</div>
                </div>
                <i class="fas fa-clock ri-flag"></i>
            </div>`;
        });
        DOM.receiptsList.innerHTML = html;
    }

    // ============================================================
    // WS message dispatch
    // ============================================================
    function handleWSMessage(msg) {
        switch (msg.type) {

            case 'pong':
                break;

            case 'connection_established': {
                State.user = {
                    id: msg.user_id, username: msg.username,
                    display_name: msg.display_name, avatar_path: msg.avatar_path
                };
                State.onlineUsers.clear();
                (msg.online_users || []).forEach(u => State.onlineUsers.set(u.id, u));
                updateSidebarMe();
                updateOnlineBadge();

                // fresh session: adopt server conversation list, drop old caches
                State.conversations.clear();
                State.conversationOrder = [];
                State.convCache.clear();
                State.messages.clear();
                State.tombstoned.clear();
                DOM.messagesList.innerHTML = '';
                (msg.conversations || []).forEach(c => upsertConversation(c));
                if (!State.conversations.has(GLOBAL_CONV)) {
                    upsertConversation({ id: GLOBAL_CONV, type: 'global', title: 'Global Chat', unread_count: 0 });
                }
                // jump back to where we were (default = Global Chat)
                const target = State.activeConvId && State.conversations.has(State.activeConvId)
                    ? State.activeConvId
                    : GLOBAL_CONV;
                State.activeConvId = null;
                switchConversation(target);
                break;
            }

            case 'conversations':
                msg.conversations.forEach(c => upsertConversation(c));
                if (State.activeConvId && State.conversations.has(State.activeConvId)) {
                    renderConversationHeader(State.activeConvId);
                }
                break;

            case 'conversation_updated':
                upsertConversation(msg.conversation);
                if (msg.conversation.id === State.activeConvId) {
                    renderConversationHeader(State.activeConvId);
                }
                break;

            case 'dm_created': {
                upsertConversation(msg.conversation);
                const peer = msg.conversation.peer;
                if (peer && peer.id !== State.user?.id) {
                    showToast(`Private chat with ${displayNameOf(peer)} created`, 'success');
                    switchConversation(msg.conversation.id);
                }
                break;
            }

            case 'new_message': {
                const m = msg.message;
                const fromMe = m.sender_id === State.user?.id;
                // Ack for a send from this tab
                if (fromMe && msg.client_id) {
                    State.pendingSends.delete(msg.client_id);
                }
                handleNewMessage(m, !!msg.client_id);
                break;
            }

            case 'messages_loaded': {
                const convId = msg.conversation_id || GLOBAL_CONV;
                const cache = getConv(convId);
                cache.loading = false;
                const wasEmpty = cache.ids.length === 0;
                const sorted = [...msg.messages].sort((a, b) => a.id - b.id);
                sorted.forEach(m => {
                    if (m.is_deleted) return;
                    if (!State.messages.has(m.id)) {
                        State.messages.set(m.id, m);
                    } else {
                        const old = State.messages.get(m.id);
                        Object.assign(old, m);
                    }
                    if (!cache.ids.includes(m.id)) cache.ids.push(m.id);
                });
                cache.ids.sort((a, b) => a - b);
                cache.hasMore = msg.has_more;
                cache.cursor = msg.next_cursor;
                if (cache.ids.length) cache.lastTs = State.messages.get(cache.ids[cache.ids.length - 1]).timestamp_ms;

                if (convId === State.activeConvId) {
                    const wasBottom = State.isAtBottom;
                    const prevHeight = DOM.messagesScroll.scrollHeight;
                    renderAllMessages(convId, {});
                    if (wasEmpty && wasBottom) scrollToBottom();
                    else if (!wasEmpty && !wasBottom && prevHeight) {
                        DOM.messagesScroll.scrollTop += DOM.messagesScroll.scrollHeight - prevHeight;
                    } else {
                        scrollToBottom();
                    }
                    DOM.loadOlderBtn.classList.toggle('hidden', !msg.has_more || !cache.cursor);
                    markActiveRead();   // respects visibility: a hidden tab must not mark messages read
                }
                break;
            }

            case 'typing_indicator': {
                if (!msg.conversation_id) break;
                if (msg.user_id === State.user?.id) break;
                // key per conversation so a "typing" in one chat never leaks into another
                const key = `${msg.conversation_id}:${msg.user_id}`;
                if (msg.is_typing) {
                    const name = msg.display_name || msg.username;
                    const existing = State.typingUsers.get(key);
                    if (existing) clearTimeout(existing.timer);
                    const timer = setTimeout(() => {
                        State.typingUsers.delete(key);
                        if (msg.conversation_id === State.activeConvId) updateTypingIndicator();
                    }, 4000);
                    State.typingUsers.set(key, { name, timer });
                } else {
                    const existing = State.typingUsers.get(key);
                    if (existing) clearTimeout(existing.timer);
                    State.typingUsers.delete(key);
                }
                if (msg.conversation_id === State.activeConvId) updateTypingIndicator();
                break;
            }

            case 'user_status': {
                if (msg.status === 'online') {
                    State.onlineUsers.set(msg.user_id, {
                        id: msg.user_id, username: msg.username,
                        display_name: msg.display_name, avatar_path: msg.avatar_path
                    });
                } else {
                    State.onlineUsers.delete(msg.user_id);
                }
                updateOnlineBadge();
                updatePeerPresence(msg.user_id, msg.status === 'online');
                if (State.activeConvId) renderConversationHeader(State.activeConvId);
                break;
            }

            case 'online_users': {
                State.onlineUsers.clear();
                msg.users.forEach(u => State.onlineUsers.set(u.id, u));
                updateOnlineBadge();
                updateAllPeerPresence();
                if (!DOM.onlineModal.classList.contains('hidden')) renderOnlineUsers();
                break;
            }

            case 'message_edited': {
                const mid = msg.message_id;
                if (State.messages.has(mid)) {
                    const m = State.messages.get(mid);
                    m.content = msg.content || m.content;
                    m.is_edited = true;
                    if (m.conversation_id === State.activeConvId) {
                        updateMessageDom(mid);
                    }
                }
                break;
            }

            case 'message_deleted': {
                const mid = msg.message_id;
                if (!State.messages.has(mid)) break;
                const m = State.messages.get(mid);
                if (m.sender_id === State.user?.id) {
                    // my own delete echoed back - row already removed by REST flow
                    removeMessageDom(mid);
                    State.messages.delete(mid);
                    pruneConvId(m.conversation_id, mid);
                } else {
                    // someone else's message vanished -> session tombstone
                    if (!State.tombstoned.has(m.conversation_id)) State.tombstoned.set(m.conversation_id, new Set());
                    State.tombstoned.get(m.conversation_id).add(mid);
                    if (m.conversation_id === State.activeConvId) {
                        updateMessageDom(mid, true);
                    }
                }
                break;
            }

            case 'message_read': {
                const mid = msg.message_id;
                const m = State.messages.get(mid);
                if (m && m.sender_id === State.user?.id) {
                    m.status = 'read';
                    if (m.conversation_id === State.activeConvId) updateTick(mid);
                }
                break;
            }

            case 'error': {
                if (msg.code === 'DUPLICATE' && msg.client_id) {
                    // our resend landed twice - the server kept the first copy
                    const pending = State.pendingSends.get(msg.client_id);
                    State.pendingSends.delete(msg.client_id);
                    const conv = (pending && pending.conversation_id) || GLOBAL_CONV;
                    if (getConv(conv).ids.length) loadMessages(conv); // re-sync quietly
                    break;
                }
                if (msg.code === 'RATE_LIMIT' && msg.client_id) {
                    // keep the message queued and try again in a moment
                    showToast('Sending too fast - retrying…', 'warning');
                    clearTimeout(window.__retrySends);
                    window.__retrySends = setTimeout(() => flushPendingSends(), 4000);
                    break;
                }
                if (msg.client_id) {
                    // fatal for this send - stop queueing it for replay
                    State.pendingSends.delete(msg.client_id);
                }
                showToast(msg.message || 'Something went wrong', 'error');
                break;
            }
        }
    }

    function pruneConvId(convId, mid) {
        const cache = State.convCache.get(convId);
        if (cache) {
            const i = cache.ids.indexOf(mid);
            if (i > -1) cache.ids.splice(i, 1);
        }
        const ts = State.tombstoned.get(convId);
        if (ts) ts.delete(mid);
    }

    function updatePeerPresence(userId, online) {
        let changed = false;
        State.conversations.forEach(c => {
            if (c.type === 'dm' && c.peer && c.peer.id === userId) {
                c.peer.online = online;
                changed = true;
            }
        });
        if (changed) renderSidebar();
        if (State.activeConvId) renderConversationHeader(State.activeConvId);
    }

    function updateAllPeerPresence() {
        let changed = false;
        State.conversations.forEach(c => {
            if (c.type === 'dm' && c.peer) {
                const on = State.onlineUsers.has(c.peer.id);
                if (c.peer.online !== on) { c.peer.online = on; changed = true; }
            }
        });
        if (changed) {
            renderSidebar();
            renderConversationHeader(State.activeConvId);
        }
    }

    // ============================================================
    // New message handling
    // ============================================================
    function handleNewMessage(m, isOwnEcho) {
        if (!m || !m.conversation_id) return;
        const convId = m.conversation_id;
        const fromMe = m.sender_id === State.user?.id;
        const cache = getConv(convId);

        if (State.messages.has(m.id)) {
            // duplicate delivery (multi-tab / replay) - just refresh the tick/status
            const existing = State.messages.get(m.id);
            if (m.status) existing.status = m.status;
            if (convId === State.activeConvId) updateTick(m.id);
            return;
        }

        State.messages.set(m.id, m);
        if (!cache.ids.includes(m.id)) cache.ids.push(m.id);
        cache.ids.sort((a, b) => a - b);
        cache.lastTs = Math.max(cache.lastTs || 0, m.timestamp_ms);
        State.latestUnseen = null;

        // keep my own sidebar summary fresh when I send
        if (fromMe) {
            const summary = convById(convId);
            if (summary && (!summary.last_message_id || m.id >= summary.last_message_id)) {
                summary.last_message_id = m.id;
                summary.last_message_ts = m.timestamp_ms;
                summary.last_message_preview = previewFromMessage(m);
                summary.last_sender_id = m.sender_id;
                summary.last_sender_name = displayNameOf(m);
                renderSidebar();
            }
        }

        const convOpen = convId === State.activeConvId;

        if (convOpen) {
            if (cache.ids.length === 1) {
                renderAllMessages(convId, { scrollBottom: true });
            } else {
                appendMessageDom(m);
                // pinned at the bottom? stay pinned. otherwise follow the pref.
                if (State.isAtBottom || State.prefs.autoScroll) scrollToBottom();
            }
            DOM.emptyState.style.display = 'none';
            if (!State.isAtBottom && !State.prefs.autoScroll && !fromMe) {
                showLatestBar(m);
            }
        } else {
            // not looking at this conversation: keep list scroll position untouched
            if (!fromMe) {
                const summary = convById(convId);
                if (summary) {
                    summary.unread_count = (summary.unread_count || 0) + 1;
                    summary.last_message_id = m.id;
                    summary.last_message_ts = m.timestamp_ms;
                    summary.last_message_preview = previewFromMessage(m);
                    summary.last_sender_id = m.sender_id;
                    summary.last_sender_name = displayNameOf(m);
                }
                renderSidebar();
            }
        }

        if (!isOwnEcho && !fromMe && State.messages.get(m.id)) {
            const wasVisible = document.visibilityState === 'visible';
            const needNotify = !convOpen || !wasVisible;
            if (needNotify) {
                playChime();
                sendDesktopNotification(m, convId);
            } else {
                // chat is open in front of you -> instant read receipt
                markActiveRead(true);
            }
        }

        if (State.activeConvId && State.conversations.has(State.activeConvId)) {
            renderConversationHeader(State.activeConvId);
        }
    }

    function previewFromMessage(m) {
        if (m.content && m.content.trim()) {
            return stripMarkdown(m.content.trim().replace(/<img\b[^>]*>/gi, ' ')).replace(/\s+/g, ' ').slice(0, 90);
        }
        if ((m.file_type || '').startsWith('image/')) return '📷 Photo';
        if ((m.file_type || '').startsWith('video/')) return '🎬 Video';
        if ((m.file_type || '').startsWith('audio/')) return '🎵 Audio';
        return '📎 ' + (m.file_name || 'File');
    }

    function stripMarkdown(text) {
        // previews (sidebar / notifications / replies) read like the message,
        // minus the formatting markers
        return String(text)
            .replace(/```/g, ' ')
            .replace(/\*\*([^*]+)\*\*/g, '$1')
            .replace(/`([^`]+)`/g, '$1')
            .replace(/(^|[\s(>])\*([^*\n]+)\*(?=[\s.,!?;:)\n]|$)/g, '$1$2');
    }

    function sendDesktopNotification(m, convId) {
        if (!('Notification' in window) || Notification.permission !== 'granted') return;
        if (m.sender_id === State.user?.id) return;
        const conv = convById(convId);
        const title = conv && conv.type === 'dm'
            ? `${displayNameOf(conv.peer)} (private chat)`
            : displayNameOf(m);
        const body = m.content
            ? truncate(stripMarkdown(m.content.replace(/<img\b[^>]*>/gi, ' ')).replace(/\s+/g, ' '), 120)
            : previewFromMessage(m);
        try {
            const n = new Notification(title, { body, tag: 'ichat-' + convId });
            n.onclick = () => {
                window.focus();
                if (convId !== State.activeConvId) switchConversation(convId);
                n.close();
            };
        } catch (e) { /* notifications blocked */ }
    }

    // ============================================================
    // Rendering messages
    // ============================================================
    function renderAllMessages(convId, opts = {}) {
        const cache = getConv(convId);
        const frag = document.createDocumentFragment();
        const ids = cache.ids;
        DOM.messagesList.innerHTML = '';

        if (!ids.length) {
            renderEmptyLoading(convId);
            return;
        }
        DOM.emptyState.style.display = 'none';

        let prevMsg = null;
        let prevDay = null;
        ids.forEach((id) => {
            const msg = State.messages.get(id);
            if (!msg) return;
            if (dayKey(msg.timestamp_ms) !== prevDay) {
                frag.appendChild(dayChip(dayKey(msg.timestamp_ms), msg.timestamp_ms));
                prevDay = dayKey(msg.timestamp_ms);
            }
            frag.appendChild(buildMessageRow(msg, prevMsg));
            prevMsg = msg;
        });
        DOM.messagesList.appendChild(frag);
        DOM.loadOlderBtn.classList.toggle('hidden', !cache.hasMore || !cache.cursor);

        if (opts.scrollBottom) scrollToBottom();
        updateScrollUI();
    }

    // relative bubble times ("now", "5m") need refreshing while the chat is open
    setInterval(() => {
        document.querySelectorAll('.message-time[data-ts]').forEach((el) => {
            el.textContent = formatTime(parseInt(el.dataset.ts, 10));
        });
    }, 30000);

    function dayChip(key, ts) {
        const div = document.createElement('div');
        div.className = 'day-separator';
        div.dataset.day = key;
        div.innerHTML = `<span class="day-chip">${escapeHtml(formatDayLabel(ts))}</span>`;
        return div;
    }

    function sameAuthor(msg, prev) {
        if (!prev) return false;
        if (msg.sender_id !== prev.sender_id) return false;
        return (msg.timestamp_ms - prev.timestamp_ms) < 5 * 60 * 1000;
    }

    function buildMessageRow(msg, prevMsg) {
        const isSelf = msg.sender_id === State.user?.id;
        const row = document.createElement('div');
        row.className = 'message-row ' + (isSelf ? 'self' : 'other');
        row.dataset.id = msg.id;
        row.dataset.conv = msg.conversation_id;
        row.innerHTML = buildMessageHTML(msg, isSelf);
        if (sameAuthor(msg, prevMsg)) row.classList.add('hug');
        return row;
    }

    function buildMessageHTML(msg, isSelf) {
        // tombstone for others' deleted messages
        const tomb = (State.tombstoned.get(msg.conversation_id) || new Set()).has(msg.id);
        if (tomb || msg.is_deleted) {
            return `<div class="deleted-note"><i class="fas fa-ban"></i> This message was deleted</div>`;
        }

        const name = displayNameOf(msg);
        const senderName = isSelf ? '' :
            `<div class="msg-sender-name">${escapeHtml(name)}</div>`;
        const avatar = isSelf
            ? '<div class="msg-avatar-col"></div>'
            : `<div class="msg-avatar-col">${avatarSmallHTML(msg)}</div>`;

        let content = '';
        if (msg.content) {
            content += `<div class="message-text">${renderInlineContent(msg.content)}</div>`;
        }
        if (msg.file_path) {
            content += buildAttachmentHTML(msg);
        }
        if (msg.reply_to_id) {
            content = buildReplyQuote(msg) + content;
        }

        const meta = buildMetaHTML(msg, isSelf);

        // actions
        let actions = '';
        actions += actionBtn('reply', 'fas fa-reply', 'Reply');
        if (!isSelf) {
            actions += `<button class="msg-action msg-action-start" data-action="startchat" title="Start a private chat"><i class="fas fa-user-lock"></i> <span class="startchat-label">Chat</span></button>`;
        } else {
            actions += actionBtn('edit', 'fas fa-pen', 'Edit');
            actions += actionBtn('delete', 'fas fa-trash', 'Delete');
        }
        actions += actionBtn('copy', 'fas fa-copy', 'Copy');

        return `
            ${avatar}
            <div class="msg-stack">
                ${senderName}
                <div class="message-bubble">${content}${meta}</div>
                <div class="message-actions">${actions}</div>
            </div>`;
    }

    function actionBtn(action, icon, label) {
        return `<button class="msg-action" data-action="${action}" title="${label}" aria-label="${label}"><i class="${icon}"></i></button>`;
    }

    function avatarSmallHTML(msg) {
        if (msg.sender_avatar_path) {
            return `<img class="msg-avatar" src="${fileUrl(msg.sender_avatar_path)}" alt="" loading="lazy">`;
        }
        return `<div class="msg-avatar-ph ${avatarColorClass(msg.sender_username || msg.sender_id)}">${initialOf(displayNameOf(msg))}</div>`;
    }

    function buildMetaHTML(msg, isSelf) {
        let s = `<div class="message-meta">`;
        s += `<span class="message-time" data-ts="${msg.timestamp_ms}">${formatTime(msg.timestamp_ms)}</span>`;
        if (msg.is_edited) s += `<span class="edited-label">edited</span>`;
        if (isSelf) {
            const read = msg.status === 'read' || (msg.reader_count > 0);
            s += `<span class="message-status${read ? ' read' : ''}" data-action="receipts" title="${read ? 'Seen — click for details' : 'Sent — click for details'}">
                    <i class="fas fa-check${read ? '-double' : ''}"></i>
                  </span>`;
        }
        s += `</div>`;
        return s;
    }

    function buildReplyQuote(msg) {
        const target = State.messages.get(msg.reply_to_id);
        const tn = target ? escapeHtml(displayNameOf(target)) : escapeHtml('Unknown');
        let text;
        if (!target) {
            text = '<em>Message unavailable</em>';
        } else {
            const tomb = (State.tombstoned.get(target.conversation_id) || new Set()).has(target.id);
            if (tomb || target.is_deleted) text = '<em>deleted message</em>';
            else text = escapeHtml(truncate(plainPreview(target), 80));
        }
        return `<span class="reply-quote" data-action="goto-reply" data-reply="${msg.reply_to_id}" title="Jump to message">
            <i class="fas fa-reply" style="font-size:0.65rem;opacity:0.8"></i>
            <span class="reply-q-name">${tn}</span>
            <span class="reply-q-text">${text}</span>
        </span>`;
    }

    function plainPreview(m) {
        let txt = m.content ? stripMarkdown(m.content.replace(/<img\b[^>]*>/gi, ' ')).replace(/\s+/g, ' ').trim() : '';
        if (!txt && m.file_path) {
            txt = (m.file_type || '').startsWith('image/') ? '📷 Photo'
                : (m.file_type || '').startsWith('video/') ? '🎬 Video'
                : (m.file_type || '').startsWith('audio/') ? '🎵 Audio'
                : '📎 ' + (m.file_name || 'File');
        }
        return txt;
    }

    function buildAttachmentHTML(msg) {
        const url = fileUrl(msg.file_path);
        const ft = (msg.file_type || '').toLowerCase();
        const fname = escapeHtml(msg.file_name || 'File');
        const opener = `data-url="${escapeHtml(url)}"`;

        if (ft.startsWith('image/')) {
            return `<div class="attachment">
                <img src="${url}" alt="${fname}" loading="lazy" decoding="async"
                     class="js-zoom" data-url="${url}" data-name="${escapeHtml(msg.file_name || 'image')}">
            </div>`;
        }
        if (ft.startsWith('video/')) {
            return `<div class="attachment">
                <video controls preload="metadata" src="${url}"></video>
            </div>`;
        }
        if (ft.startsWith('audio/')) {
            return `<div class="attachment"><audio controls preload="metadata" src="${url}"></audio></div>`;
        }
        const icon = ft.includes('pdf') ? 'fa-file-pdf' : ft.startsWith('text') ? 'fa-file-alt' : 'fa-file';
        return `<div class="attachment-file" data-action="open-file" ${opener} title="Open ${fname}">
            <i class="fas ${icon} file-card-icon"></i>
            <div class="file-info">
                <div class="file-name">${fname}</div>
                <div class="file-size">${formatFileSize(msg.file_size)}</div>
            </div>
            <i class="fas fa-download" style="color:var(--text-tertiary);font-size:0.8rem"></i>
        </div>`;
    }

    // ---------------- content formatting ----------------
    const TOKEN_PLACEHOLDER = '\u0001';

    function renderInlineContent(raw) {
        let text = String(raw).replace(/\r\n/g, '\n');
        const parts = [];
        const ph = (i) => TOKEN_PLACEHOLDER + i + TOKEN_PLACEHOLDER;

        // fenced code blocks ``` ... ``` (their contents are never re-parsed)
        text = text.replace(/```([\s\S]*?)```/g, (whole, code) => {
            const clean = code.replace(/^\n/, '').replace(/\n$/, '');
            parts.push(`<pre class="code-block"><code>${escapeHtml(clean)}</code></pre>`);
            return ph(parts.length - 1);
        });

        // html <img> tags (the requested embed feature)
        text = text.replace(/<img\b[^>]*>/gi, (whole) => {
            const srcMatch = whole.match(/\bsrc\s*=\s*["']([^"']+)["']/i);
            const src = srcMatch && srcMatch[1];
            if (src && isAllowedImageSrc(src)) {
                const s = escapeHtml(src);
                parts.push(`<img class="embedded-img js-zoom" src="${s}" data-url="${s}" data-name="embedded image" loading="lazy" decoding="async" referrerpolicy="no-referrer" alt="">`);
            } else {
                // not a safe embed - show it as literal text (escaped exactly once)
                parts.push(escapeHtml(whole));
            }
            return ph(parts.length - 1);
        });

        // auto-linkify http(s) URLs while the text is still raw, so query
        // strings with &amp; / entities stay intact inside hrefs
        text = text.replace(/(https?:\/\/[^\s<>"']+)/gi, (url) => {
            let clean = url;
            const trailing = clean.match(/[.,!?;:)\]}]+$/);
            if (trailing) clean = clean.slice(0, -trailing[0].length);
            if (!/^https?:\/\/\S+\.\S+/.test(clean) && !/^https?:\/\/localhost/i.test(clean)) return url;
            const c = escapeHtml(clean);
            parts.push(`<a href="${c}" target="_blank" rel="noopener noreferrer nofollow">${c}</a>`);
            return ph(parts.length - 1) + (trailing ? trailing[0] : '');
        });

        // escape everything once, then apply light markdown on the escaped text
        // (placeholders contain no markdown characters, so they stay inert),
        // and only then splice the generated HTML (code/img/link) back in.
        let out = escapeHtml(text);
        out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
        out = out.replace(/`([^`\n]+)`/g, '<code>$1</code>');
        out = out.replace(/(^|[\s(>])\*([^*\n]+)\*(?=[\s.,!?;:)\n]|$)/g, '$1<em>$2</em>');
        parts.forEach((html, i) => {
            out = out.split(ph(i)).join(html);
        });
        return out;
    }

    function isAllowedImageSrc(src) {
        if (!src) return false;
        if (src.startsWith('/api/download/')) return true;
        if (/^https?:\/\//i.test(src)) return true;
        if (/^data:image\/(png|jpe?g|gif|webp);base64,/i.test(src)) return src.length < 200000;
        return false;
    }

    // ---------------- DOM patching ----------------
    function rowEl(id) {
        return DOM.messagesList.querySelector(`.message-row[data-id="${id}"]`);
    }

    function updateMessageDom(id, asTombstone = false) {
        const el = rowEl(id);
        const msg = State.messages.get(id);
        if (!el || !msg) return;
        const isSelf = msg.sender_id === State.user?.id;
        const tomb = asTombstone || (State.tombstoned.get(msg.conversation_id) || new Set()).has(id);
        el.className = 'message-row ' + (isSelf ? 'self' : 'other') + (tomb ? ' tomb' : '');
        el.innerHTML = buildMessageHTML(msg, isSelf);
        // keep hug class if previous row same author
        const prev = el.previousElementSibling;
        if (prev && prev.classList && prev.classList.contains('message-row')) {
            const prevId = parseInt(prev.dataset.id, 10);
            const prevMsg = State.messages.get(prevId);
            if (prevMsg && sameAuthor(msg, prevMsg)) el.classList.add('hug');
        }
    }

    function removeMessageDom(id) {
        const el = rowEl(id);
        if (el) el.remove();
        // clean dangling day chips
        DOM.messagesList.querySelectorAll('.day-separator').forEach((chip) => {
            const next = chip.nextElementSibling;
            if (!next || next.classList.contains('day-separator')) chip.remove();
        });
        if (!DOM.messagesList.querySelector('.message-row') &&
            !DOM.messagesList.querySelector('.deleted-note')) {
            renderEmptyLoading(State.activeConvId);
        }
    }

    function updateTick(id) {
        const el = rowEl(id);
        if (!el) return;
        const status = el.querySelector('.message-status');
        if (status) {
            status.classList.add('read');
            const i = status.querySelector('i');
            if (i) i.className = 'fas fa-check-double';
        }
    }

    function appendMessageDom(m) {
        const ids = getConv(m.conversation_id).ids;
        const prevId = ids.length > 1 ? ids[ids.length - 2] : null;
        const prevMsg = prevId ? State.messages.get(prevId) : null;
        const dayChanged = !prevMsg || dayKey(prevMsg.timestamp_ms) !== dayKey(m.timestamp_ms);

        if (dayChanged) {
            DOM.messagesList.appendChild(dayChip(dayKey(m.timestamp_ms), m.timestamp_ms));
        }
        const row = buildMessageRow(m, prevMsg);
        if (prevMsg && !dayChanged && sameAuthor(m, prevMsg)) row.classList.add('hug');
        DOM.messagesList.appendChild(row);
        DOM.emptyState.style.display = 'none';
    }

    // ============================================================
    // Scroll UI
    // ============================================================
    function updateScrollUI() {
        if (!State.isAtBottom) {
            DOM.scrollBottomBtn.classList.toggle('hidden', !State.prefs.showScrollBtn);
        } else {
            DOM.scrollBottomBtn.classList.add('hidden');
            hideLatestBar();
        }
    }

    DOM.messagesScroll.addEventListener('scroll', () => {
        const { scrollTop, scrollHeight, clientHeight } = DOM.messagesScroll;
        State.isAtBottom = (scrollHeight - scrollTop - clientHeight) < 90;
        updateScrollUI();
        if (State.isAtBottom) hideLatestBar();
    });

    DOM.scrollBottomBtn.addEventListener('click', () => { scrollToBottom(); hideLatestBar(); });

    function scrollToBottom() {
        requestAnimationFrame(() => {
            DOM.messagesScroll.scrollTop = DOM.messagesScroll.scrollHeight;
            State.isAtBottom = true;
            updateScrollUI();
        });
    }

    // ============================================================
    // Latest message bar
    // ============================================================
    function showLatestBar(m) {
        if (!State.prefs.showLatestBar || !DOM.latestMessageBar) return;
        const name = displayNameOf(m);
        if (m.sender_avatar_path) {
            DOM.lmbAvatar.innerHTML = `<img src="${fileUrl(m.sender_avatar_path)}" alt="">`;
        } else {
            DOM.lmbAvatar.textContent = initialOf(name);
        }
        DOM.lmbName.textContent = name + ':';
        DOM.lmbText.textContent = truncate(plainPreview(m), 40);
        DOM.latestMessageBar.classList.remove('hidden');
    }

    function hideLatestBar() {
        if (DOM.latestMessageBar) DOM.latestMessageBar.classList.add('hidden');
        State.latestUnseen = null;
    }

    DOM.latestMessageBar.addEventListener('click', () => {
        scrollToBottom();
        hideLatestBar();
        markActiveRead(true);
    });

    // ============================================================
    // Message row actions (event delegation)
    // ============================================================
    DOM.messagesList.addEventListener('click', (e) => {
        const row = e.target.closest('.message-row');
        if (!row) return;
        const msgId = parseInt(row.dataset.id, 10);
        const msg = State.messages.get(msgId);
        if (!msg) return;

        const actionEl = e.target.closest('[data-action]');
        if (!actionEl) return;
        const action = actionEl.dataset.action;
        const urlEl = e.target.closest('.js-zoom, [data-url]');
        const zoomUrl = urlEl ? (urlEl.dataset.url || urlEl.getAttribute('src')) : null;

        switch (action) {
            case 'reply':
                replyTo(msgId);
                break;
            case 'startchat':
                openConvModal(msg.sender_id, displayNameOf(msg));
                break;
            case 'edit':
                if (msg.sender_id === State.user?.id) startInlineEdit(msgId);
                break;
            case 'delete':
                if (msg.sender_id === State.user?.id) requestDelete(msg);
                break;
            case 'copy':
                copyMessage(msg);
                break;
            case 'receipts':
                openReceipts(msgId);
                break;
            case 'goto-reply': {
                const targetId = parseInt(actionEl.dataset.reply, 10);
                const targetRow = rowEl(targetId);
                if (targetRow) {
                    targetRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    targetRow.style.transition = 'background 0.8s';
                    targetRow.style.background = 'var(--primary-light)';
                    setTimeout(() => { targetRow.style.background = ''; }, 1200);
                } else {
                    showToast('That message is no longer loaded', 'info');
                }
                break;
            }
            case 'open-file':
                if (zoomUrl) window.open(zoomUrl, '_blank', 'noopener');
                break;
        }
    });

    // image zoom (attachment or embedded)
    document.addEventListener('click', (e) => {
        const img = e.target.closest('img.js-zoom');
        if (!img) return;
        e.stopPropagation();
        const url = img.dataset.url || img.getAttribute('src');
        const name = img.dataset.name || 'image';
        DOM.lightboxImg.src = url;
        DOM.lightboxImg.referrerPolicy = 'no-referrer';
        DOM.lightboxName.textContent = name;
        DOM.lightboxDownload.href = url;
        openModal(DOM.lightboxModal);
    });

    document.addEventListener('click', (e) => {
        const fileCard = e.target.closest('.attachment-file');
        if (fileCard && !e.target.closest('[data-action]')) {
            const a = fileCard.querySelector('a, [data-url]');
            if (a && a.dataset.url) window.open(a.dataset.url, '_blank', 'noopener');
        }
    });

    function copyMessage(msg) {
        const text = plainPreview(msg);
        const done = () => showToast('Copied to clipboard', 'success');
        const fallback = () => {
            const ta = document.createElement('textarea');
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); done(); } catch (err) { showToast('Could not copy', 'error'); }
            ta.remove();
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(fallback);
        } else fallback();
    }

    // ============================================================
    // Reply
    // ============================================================
    function replyTo(messageId) {
        const msg = State.messages.get(messageId);
        if (!msg) return;
        State.replyTo = msg;
        DOM.replyUser.textContent = displayNameOf(msg);
        DOM.replyText.textContent = truncate(plainPreview(msg) || '📎 attachment', 60);
        DOM.replyPreview.classList.remove('hidden');
        DOM.messageInput.focus();
    }
    function clearReply() {
        State.replyTo = null;
        DOM.replyPreview.classList.add('hidden');
    }
    document.addEventListener('click', (e) => {
        if (e.target.closest('#cancel-reply')) clearReply();
    });

    // ============================================================
    // Edit (inline, no more prompt())
    // ============================================================
    function startInlineEdit(messageId) {
        if (State.editingId) cancelInlineEdit();
        const msg = State.messages.get(messageId);
        if (!msg) return;
        State.editingId = messageId;
        const el = rowEl(messageId);
        if (!el) return;
        const bubble = el.querySelector('.message-bubble');
        bubble.innerHTML = `<div class="inline-edit-box">
            <textarea maxlength="${10000}">${escapeHtml(msg.content || '')}</textarea>
            <div class="inline-edit-actions">
                <button class="btn btn-ghost btn-sm" data-edit="cancel"><i class="fas fa-times"></i> Cancel</button>
                <button class="btn btn-primary btn-sm" data-edit="save"><i class="fas fa-check"></i> Save</button>
            </div>
        </div>`;
        const ta = bubble.querySelector('textarea');
        ta.focus();
        ta.setSelectionRange(ta.value.length, ta.value.length);

        bubble.querySelector('[data-edit="save"]').addEventListener('click', () => saveInlineEdit(messageId, ta.value));
        bubble.querySelector('[data-edit="cancel"]').addEventListener('click', () => cancelInlineEdit());
        ta.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); saveInlineEdit(messageId, ta.value); }
            if (e.key === 'Escape') cancelInlineEdit();
        });
    }

    function cancelInlineEdit() {
        const id = State.editingId;
        State.editingId = null;
        if (id && State.messages.has(id)) updateMessageDom(id);
    }

    async function saveInlineEdit(id, content) {
        content = content.trim();
        if (!content) { showToast('Message cannot be empty', 'warning'); return; }
        const msg = State.messages.get(id);
        if (!msg) return;
        try {
            const res = await fetch(`${API}/api/messages/${id}?content=${encodeURIComponent(content)}`, {
                method: 'PATCH',
                headers: { 'X-Auth-Token': State.token }
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || 'Edit failed');
            msg.content = content;
            msg.is_edited = true;
            State.editingId = null;
            updateMessageDom(id);
            const convId = msg.conversation_id;
            const summary = convById(convId);
            if (summary && summary.last_message_id === id) {
                summary.last_message_preview = truncate(content.replace(/\s+/g, ' '), 90);
                renderSidebar();
            }
        } catch (err) {
            showToast(err.message, 'error');
        }
    }

    // ============================================================
    // Delete (HTTP - reliable, never relies on a fragile WS send)
    // ============================================================
    async function requestDelete(msg) {
        const ok = await showConfirm({
            title: 'Delete message',
            message: 'This permanently deletes the message for everyone. This cannot be undone.',
            okText: 'Delete'
        });
        if (!ok) return;
        try {
            const res = await fetch(`${API}/api/messages/${msg.id}`, {
                method: 'DELETE',
                headers: { 'X-Auth-Token': State.token }
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                throw new Error(data.detail || 'Delete failed');
            }
            removeMessageDom(msg.id);
            State.messages.delete(msg.id);
            pruneConvId(msg.conversation_id, msg.id);
            if (State.replyTo && State.replyTo.id === msg.id) clearReply();
            showToast('Message deleted', 'success');
            const summary = convById(msg.conversation_id);
            if (summary && summary.last_message_id === msg.id) {
                summary.last_message_preview = '';
                summary.last_message_ts = null;
                renderSidebar();
            }
        } catch (err) {
            showToast(err.message, 'error');
        }
    }

    // ============================================================
    // Compose / send
    // ============================================================
    DOM.composeForm.addEventListener('submit', (e) => { e.preventDefault(); sendMessage(); });

    DOM.messageInput.addEventListener('input', () => {
        const hasContent = DOM.messageInput.value.trim().length > 0 || !!State.pendingFile;
        DOM.sendBtn.disabled = !hasContent;
        sendTyping(true);
        autoResize();
    });

    DOM.messageInput.addEventListener('keydown', (e) => {
        const enterSends = State.prefs.enterSend;
        if (e.key === 'Enter' && !e.shiftKey && enterSends) {
            e.preventDefault();
            sendMessage();
        } else if (e.key === 'Enter' && !e.shiftKey && !enterSends) {
            // plain Enter = newline; Ctrl/Cmd+Enter still sends
            if (e.ctrlKey || e.metaKey) { e.preventDefault(); sendMessage(); }
        }
    });

    DOM.messageInput.addEventListener('blur', () => sendTyping(false));

    function sendMessage() {
        const content = DOM.messageInput.value.trim();
        if (!content && !State.pendingFile) return;
        if (!State.activeConvId) { showToast('No conversation selected', 'warning'); return; }
        if (!wsOpen()) {
            showToast('Not connected - message queued, sending when reconnected', 'warning');
        }
        const clientId = generateId();
        const payload = {
            type: 'send_message',
            content: content,
            conversation_id: State.activeConvId,
            reply_to_id: State.replyTo ? State.replyTo.id : null,
            client_id: clientId
        };
        if (State.pendingFile) {
            payload.file_path = State.pendingFile.file_path;
            payload.file_type = State.pendingFile.file_type;
            payload.file_name = State.pendingFile.file_name;
            payload.file_size = State.pendingFile.file_size;
        }
        State.pendingSends.set(clientId, payload);
        // only keep the most recent 30 queued messages
        while (State.pendingSends.size > 30) {
            State.pendingSends.delete(State.pendingSends.keys().next().value);
        }

        if (!wsSend(payload)) {
            // offline: keep queued; UI clears and message re-sends on reconnect
            showToast('Message queued - will send when you reconnect', 'warning');
        }
        DOM.messageInput.value = '';
        DOM.sendBtn.disabled = true;
        DOM.messageInput.dispatchEvent(new Event('input'));
        clearReply();
        clearPendingFile();
        sendTyping(false);
        autoResize();
    }

    function flushPendingSends() {
        const pending = [...State.pendingSends.entries()];
        pending.forEach(([clientId, payload]) => {
            if (payload.conversation_id === GLOBAL_CONV && !getConv(GLOBAL_CONV).ids.length) {
                // ensure lobby is loaded before replaying
                loadMessages(GLOBAL_CONV);
            }
            wsSend({ ...payload });
        });
    }

    function sendTyping(isTyping) {
        if (!wsOpen()) return;
        const now = Date.now();
        if (isTyping && now - State.typingSentAt < 1600) return;
        State.typingSentAt = now;
        wsSend({ type: 'typing', is_typing: isTyping, conversation_id: State.activeConvId });
        if (isTyping) {
            clearTimeout(window.__typingStop);
            window.__typingStop = setTimeout(() => sendTyping(false), 2200);
        }
    }

    function updateTypingIndicator() {
        const prefix = `${State.activeConvId}:`;
        const names = [...State.typingUsers.entries()]
            .filter(([k]) => k.startsWith(prefix))
            .map(([, v]) => v.name);
        DOM.typingIndicator.classList.toggle('hidden', names.length === 0);
        if (names.length) {
            DOM.typingText.textContent = names.join(', ') + (names.length === 1 ? ' is typing…' : ' are typing…');
        }
    }

    function autoResize() {
        DOM.messageInput.style.height = 'auto';
        DOM.messageInput.style.height = Math.min(DOM.messageInput.scrollHeight, 120) + 'px';
    }

    // ============================================================
    // Start private chat modal
    // ============================================================
    function openConvModal(peerId, peerName) {
        if (peerId === State.user?.id) { showToast('That is you!', 'info'); return; }
        const existing = [...State.conversations.values()].filter(
            c => c.type === 'dm' && c.peer && c.peer.id === peerId
        );
        DOM.convModalDesc.innerHTML = `Chat privately with <strong>${escapeHtml(peerName)}</strong>` +
            (existing.length ? ` (${existing.length} of 3 existing)` : '');
        DOM.convModal.dataset.peerId = peerId;
        DOM.convModal.dataset.peerName = peerName;
        renderConvModal(existing);
        openModal(DOM.convModal);
    }

    function renderConvModal(existing) {
        let html = '';
        existing.forEach(c => {
            html += `<button class="conv-modal-item" data-open-conv="${c.id}">
                ${avatarHTML(c, 36)}
                <div class="mi-info">
                    <span class="mi-name">${escapeHtml(dmLabel(c))}</span>
                    <span class="mi-preview">${previewHTML(c)}</span>
                </div>
                <i class="fas fa-arrow-right mi-open"></i>
            </button>`;
        });
        if (!existing.length) {
            html = '<div class="conv-modal-empty">No private chats with this person yet</div>';
        }
        DOM.convModalList.innerHTML = html;
        const atLimit = existing.length >= 3;
        DOM.newChatBtn.disabled = atLimit;
        DOM.newChatBtn.querySelector('i').className = atLimit ? 'fas fa-ban' : 'fas fa-plus';
        DOM.newChatBtn.lastChild.textContent = atLimit ? ' Limit Reached' : ' Start New Chat';
        if (atLimit) {
            const note = document.createElement('div');
            note.className = 'conv-modal-note';
            note.id = 'conv-limit-note';
            note.innerHTML = '<i class="fas fa-info-circle"></i> You already have 3 private chats with this person. The limit is 3 per person.';
            if (!DOM.convModalList.querySelector('#conv-limit-note')) DOM.convModalList.appendChild(note);
        } else {
            const note = DOM.convModalList.querySelector('#conv-limit-note');
            if (note) note.remove();
        }
        DOM.convModalList.querySelectorAll('[data-open-conv]').forEach(btn => {
            btn.addEventListener('click', () => {
                const cid = parseInt(btn.dataset.openConv, 10);
                closeModal(DOM.convModal);
                switchConversation(cid);
            });
        });
    }

    DOM.newChatBtn.addEventListener('click', async () => {
        const peerId = parseInt(DOM.convModal.dataset.peerId, 10);
        const peerName = DOM.convModal.dataset.peerName;
        if (!peerId) return;
        const btn = DOM.newChatBtn;
        btn.disabled = true;
        btn.innerHTML = '<div class="spinner spinner-sm"></div> Creating…';
        const finish = () => { btn.disabled = false; btn.innerHTML = '<i class="fas fa-plus"></i> Start New Chat'; };
        try {
            if (wsOpen()) {
                wsSend({ type: 'create_dm', user_id: peerId });
                // The dm_created event switches conversation automatically.
                // Close the modal: outcome arrives via ws in ms.
                closeModal(DOM.convModal);
                finish();
                return;
            }
            const res = await fetch(`${API}/api/conversations/dm?user_id=${peerId}`, {
                method: 'POST', headers: { 'X-Auth-Token': State.token }
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || 'Could not start chat');
            upsertConversation(data.conversation);
            closeModal(DOM.convModal);
            switchConversation(data.conversation.id);
            showToast(`Private chat with ${peerName} started`, 'success');
        } catch (err) {
            showToast(err.message, 'error');
            finish();
        }
    });

    // ============================================================
    // Uploads (attach, paste, drag & drop)
    // ============================================================
    DOM.attachBtn.addEventListener('click', () => DOM.fileInput.click());
    DOM.fileInput.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        e.target.value = '';
        if (!file) return;
        await uploadChunked(file);
    });

    DOM.messageInput.addEventListener('paste', (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        for (const item of items) {
            if (item.type && item.type.startsWith('image/')) {
                e.preventDefault();
                const file = item.getAsFile();
                if (file) uploadChunked(file);
                break;
            }
        }
    });

    // drag & drop onto the chat window
    let dragDepth = 0;
    DOM.chatScreen.addEventListener('dragenter', (e) => {
        if (!e.dataTransfer || ![...(e.dataTransfer.types || [])].includes('Files')) return;
        dragDepth++;
        DOM.dropOverlay.classList.remove('hidden');
    });
    DOM.chatScreen.addEventListener('dragover', (e) => { e.preventDefault(); });
    DOM.chatScreen.addEventListener('dragleave', () => {
        dragDepth = Math.max(0, dragDepth - 1);
        if (!dragDepth) DOM.dropOverlay.classList.add('hidden');
    });
    DOM.chatScreen.addEventListener('drop', async (e) => {
        e.preventDefault();
        dragDepth = 0;
        DOM.dropOverlay.classList.add('hidden');
        const files = e.dataTransfer && e.dataTransfer.files;
        if (!files || !files.length) return;
        const file = files[0];
        if (files.length > 1) showToast('Attaching the first file (one per message)', 'info');
        await uploadChunked(file);
    });

    async function uploadChunked(file) {
        if (file.size > 500 * 1024 * 1024) {
            showToast('File too large (max 500MB)', 'error');
            return;
        }
        const CHUNK_SIZE = 5 * 1024 * 1024;
        const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));
        const uploadId = generateId();
        DOM.uploadProgress.classList.remove('hidden');
        updateProgress(0, file.name, file.size);

        for (let i = 0; i < totalChunks; i++) {
            const blob = file.slice(i * CHUNK_SIZE, Math.min((i + 1) * CHUNK_SIZE, file.size));
            const formData = new FormData();
            formData.append('file', blob);
            const url = `${API}/api/upload/chunk?chunk_index=${i}&total_chunks=${totalChunks}` +
                `&file_name=${encodeURIComponent(file.name)}&file_type=${encodeURIComponent(file.type || 'application/octet-stream')}` +
                `&file_size=${file.size}&upload_id=${uploadId}`;
            try {
                const res = await fetch(url, {
                    method: 'POST', body: formData, headers: { 'X-Auth-Token': State.token }
                });
                if (!res.ok) {
                    const data = await res.json().catch(() => ({}));
                    throw new Error(data.detail || 'Upload failed');
                }
                const data = await res.json();
                updateProgress(((i + 1) / totalChunks) * 100, file.name, file.size);
                if (data.status === 'complete') {
                    State.pendingFile = {
                        file_path: data.file_path,
                        file_type: data.file_type || file.type,
                        file_name: data.file_name,
                        file_size: data.file_size
                    };
                    showUploadPreview(State.pendingFile);
                    DOM.uploadProgress.classList.add('hidden');
                    DOM.sendBtn.disabled = false;
                    DOM.messageInput.focus();
                    return;
                }
            } catch (err) {
                showToast('Upload failed: ' + err.message, 'error');
                DOM.uploadProgress.classList.add('hidden');
                return;
            }
        }
        DOM.uploadProgress.classList.add('hidden');
    }

    function updateProgress(percent, filename, size) {
        DOM.progressFill.style.width = `${percent}%`;
        DOM.progressText.textContent = `${Math.round(percent)}%`;
        if (filename) DOM.uploadFilename.textContent = filename;
        if (size !== undefined) DOM.uploadSize.textContent = formatFileSize(size);
    }

    function showUploadPreview(pf) {
        const ft = (pf.file_type || '').toLowerCase();
        DOM.uploadIcon.className = 'fas ' + (ft.startsWith('image/') ? 'fa-image'
            : ft.startsWith('video/') ? 'fa-video'
            : ft.startsWith('audio/') ? 'fa-music' : 'fa-paperclip');
        DOM.uploadFilename.textContent = pf.file_name;
        DOM.uploadSize.textContent = formatFileSize(pf.file_size);
        DOM.uploadPreview.classList.remove('hidden');
    }

    function clearPendingFile() {
        State.pendingFile = null;
        DOM.uploadPreview.classList.add('hidden');
    }

    DOM.cancelUpload.addEventListener('click', () => {
        clearPendingFile();
        DOM.sendBtn.disabled = DOM.messageInput.value.trim().length === 0;
    });

    // ============================================================
    // Emoji picker
    // ============================================================
    DOM.emojiBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        DOM.emojiPicker.classList.toggle('hidden');
        if (!DOM.emojiPicker.classList.contains('hidden')) renderEmojis('smileys');
    });
    document.addEventListener('click', (e) => {
        if (!DOM.emojiPicker.contains(e.target) && e.target !== DOM.emojiBtn) {
            DOM.emojiPicker.classList.add('hidden');
        }
    });

    const emojiSets = {
        smileys: ['😀','😃','😄','😁','😅','😂','🤣','😊','😇','🙂','😉','😌','😍','🥰','😘','😗','😋','😛','😜','🤪','😝','🤑','🤗','🤭','🤫','🤔','🤐','🤨','😐','😑','😶','😏','😒','🙄','😬','😮','🤥','😔','😪','🤤','😴','😷','🤒','🤕','🤢','🤮','🤧','😵','🤯','🥵','🥶','😎','🤓','🧐','😕','😟','🙁','😮','😯','😲','😳','🥺','😦','😧','😨','😰','😥','😢','😭','😱','😖','😣','😞','😓','😩','😫','🥱','😤','😡','😠','🤬','😈','👿','💀','☠️','💩','🤡','👹','👺','👻','👽','👾','🤖'],
        people: ['👶','🧒','👦','👧','🧑','👱','👨','🧔','👩','🧓','👴','👵','👲','👳','🧕','👮','👷','💂','🕵️','👨‍⚕️','👩‍⚕️','👨‍🎓','👩‍🎓','👨‍🏫','👩‍🏫','👨‍⚖️','👩‍⚖️','👨‍🌾','👩‍🌾','👨‍🍳','👩‍🍳','👨‍🔧','👩‍🔧','👨‍🏭','👩‍🏭','👨‍💼','👩‍💼','👨‍🔬','👩‍🔬','👨‍💻','👩‍💻','👨‍🎤','👩‍🎤','👨‍🎨','👩‍🎨','👨‍✈️','👩‍✈️','👨‍🚀','👩‍🚀','👨‍🚒','👩‍🚒'],
        animals: ['🐶','🐱','🐭','🐹','🐰','🦊','🐻','🐼','🐨','🐯','🦁','🐮','🐷','🐸','🐵','🙈','🙉','🙊','🐒','🐔','🐧','🐦','🐤','🦆','🦅','🦉','🦇','🐺','🐗','🐴','🦄','🐝','🐛','🦋','🐌','🐞','🐜','🦟','🦗','🕷️','🦂','🐢','🐍','🦎','🐙','🦑','🦐','🦞','🦀','🐡','🐠','🐟','🐬','🐳','🐋','🦈','🐊','🐅','🐆','🦓','🦍','🐘','🦛','🦏','🐪','🐫','🦒','🦘','🐃','🐂','🐄','🐎','🐖','🐏','🐑','🦙','🐐','🦌','🐕','🐩','🐈','🐓','🦃','🦚','🦜','🦢','🦩','🕊️','🐇','🦝','🦨','🦡','🦦','🦥','🐁','🐀','🐿️','🦔'],
        food: ['🍏','🍎','🍐','🍊','🍋','🍌','🍉','🍇','🍓','🫐','🍈','🍒','🍑','🥭','🍍','🥥','🥝','🍅','🍆','🥑','🥦','🥬','🥒','🌶️','🌽','🥕','🧄','🧅','🥔','🍠','🥐','🍞','🥖','🥨','🧀','🥚','🍳','🧈','🥞','🧇','🥓','🥩','🍗','🍖','🌭','🍔','🍟','🍕','🥪','🥙','🧆','🌮','🌯','🥗','🥘','🥫','🍝','🍜','🍲','🍛','🍣','🍱','🥟','🦪','🍤','🍙','🍚','🍘','🍥','🥠','🥮','🍢','🍡','🍧','🍨','🍦','🥧','🧁','🍰','🎂','🍮','🍭','🍬','🍫','🍿','🍩','🍪','🌰','🥜','🍯','🥛','🍼','☕','🍵','🧃','🥤','🧋','🍺','🍻','🥂','🍷','🥃','🍸','🍹','🍾'],
        activities: ['⚽','🏀','🏈','⚾','🥎','🎾','🏐','🏉','🥏','🎱','🏓','🏸','🏒','🏑','🥍','🏏','🥅','⛳','🏹','🎣','🤿','🥊','🥋','🎽','🛹','🛼','🛷','⛸️','🥌','🎿','⛷️','🏂','🪂','🏋️','🤼','🤸','⛹️','🤾','🏌️','🏇','🧘','🏄','🏊','🤽','🚣','🧗','🚵','🚴','🏆','🥇','🥈','🥉','🏅','🎖️','🏵️','🎗️','🎫','🎟️','🎪','🤹','🎭','🩰','🎨','🎬','🎤','🎧','🎼','🎹','🥁','🎷','🎺','🎸','🎻','🎲','♟️','🎯','🎳','🎮','🎰','🧩'],
        objects: ['⌚','📱','💻','⌨️','🖥️','🖨️','🖱️','💽','💾','💿','📀','📼','📷','📸','📹','🎥','📞','☎️','📺','📻','🧭','⏱️','⏲️','⏰','🕰️','⌛','📡','🔋','🔌','💡','🔦','🕯️','🧯','💸','💵','💴','💶','💷','🪙','💰','💳','💎','⚖️','🧰','🔧','🔨','⚒️','🛠️','⛏️','🪚','🔩','⚙️','🧲','🔫','💣','🧨','🔪','🗡️','⚔️','🛡️','🚬','⚰️','🪦','⚱️','🏺','🔮','📿','💈','⚗️','🔭','🔬','💊','💉','🩸','🧬','🦠','🧫','🧪','🌡️','🧹','🧺','🧻','🚽','🚰','🚿','🛁','🧼','🪥','🪒','🧽','🪣','🧴','🪢','🧷'],
        symbols: ['❤️','🧡','💛','💚','💙','💜','🖤','🤍','🤎','💔','❣️','💕','💞','💓','💗','💖','💘','💝','💟','☮️','✝️','☪️','🕉️','☸️','✡️','🔯','🕎','☯️','☦️','🛐','⛎','♈','♉','♊','♋','♌','♍','♎','♏','♐','♑','♒','♓','🆔','⚛️','☢️','☣️','📴','📳','🈶','🈚','🈸','🈺','🈷️','✴️','🆚','💮','🉐','㊙️','㊗️','🈴','🈵','🈹','🈲','🅰️','🅱️','🆎','🆑','🅾️','🆘','❌','⭕','🛑','⛔','📛','🚫','💯','💢','♨️','❗','❕','❓','❔','‼️','⁉️','⚠️','🚸','🔱','⚜️','🔰','♻️','✅','❇️','✳️','❎','🌐','💠','Ⓜ️','🌀','💤','🏧','♿','🅿️','🚹','🚺','🚻','🚮','🎦','📶','ℹ️','🔤','🔡','🔠','🆖','🆗','🆙','🆒','🆕','🆓','0️⃣','1️⃣','2️⃣','3️⃣','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣','🔟','#️⃣','*️⃣','▶️','⏸️','⏹️','⏺️','⏭️','⏮️','⏩','⏪','⏫','⏬','◀️','🔼','🔽','➡️','⬅️','⬆️','⬇️','↗️','↘️','↙️','↖️','↕️','↔️','↪️','↩️','⤴️','⤵️','🔀','🔁','🔂','🔄','🔃','🎵','🎶','➕','➖','➗','✖️','♾️','💲','💱','™️','©️','®️','〰️','➰','➿','✔️','☑️'],
        flags: ['🏁','🚩','🎌','🏴','🏳️','🏳️‍🌈','🏳️‍⚧️','🏴‍☠️','🇺🇸','🇬🇧','🇨🇦','🇦🇺','🇩🇪','🇫🇷','🇯🇵','🇨🇳','🇷🇺','🇧🇷','🇮🇳','🇰🇷','🇪🇸','🇮🇹','🇲🇽','🇿🇦','🇸🇪','🇳🇴','🇩🇰','🇫🇮','🇳🇱','🇧🇪','🇦🇹','🇨🇭','🇵🇱','🇵🇹','🇬🇷','🇹🇷','🇸🇦','🇦🇪','🇮🇱','🇮🇷','🇵🇰','🇧🇩','🇻🇳','🇹🇭','🇮🇩','🇲🇾','🇵🇭','🇸🇬','🇳🇿','🇦🇷','🇨🇱','🇨🇴','🇵🇪','🇻🇪','🇺🇾','🇪🇨','🇧🇴','🇵🇾','🇬🇹','🇨🇺','🇩🇴']
    };

    document.querySelectorAll('.emoji-cat').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            document.querySelectorAll('.emoji-cat').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderEmojis(btn.dataset.cat);
        });
    });

    function renderEmojis(category) {
        const emojis = emojiSets[category] || [];
        DOM.emojiGrid.innerHTML = emojis.map(e => `<button class="emoji-item" type="button">${e}</button>`).join('');
        DOM.emojiGrid.querySelectorAll('.emoji-item').forEach(btn => {
            btn.addEventListener('click', () => {
                DOM.messageInput.value += btn.textContent;
                DOM.messageInput.focus();
                DOM.messageInput.dispatchEvent(new Event('input'));
                DOM.emojiPicker.classList.add('hidden');
            });
        });
    }

    // ============================================================
    // Load older
    // ============================================================
    DOM.loadOlderBtn.addEventListener('click', () => {
        const convId = State.activeConvId;
        const cache = getConv(convId);
        if (wsOpen() && cache.hasMore && cache.cursor && !cache.loading) {
            cache.loading = true;
            loadMessages(convId, { older: true });
        }
    });

    // ============================================================
    // Online users
    // ============================================================
    DOM.onlineBtn.addEventListener('click', () => {
        renderOnlineUsers();
        openModal(DOM.onlineModal);
        requestOnlineUsers();
    });

    function requestOnlineUsers() {
        if (wsOpen()) wsSend({ type: 'get_online_users' });
    }

    function updateOnlineBadge() {
        DOM.onlineBadge.textContent = State.onlineUsers.size;
        DOM.onlineBadge.classList.toggle('hidden', State.onlineUsers.size === 0);
    }

    function renderOnlineUsers() {
        DOM.onlineUsersList.innerHTML = '';
        if (!State.onlineUsers.size) {
            DOM.onlineUsersList.innerHTML = '<div class="loading-state"><span>No users online</span></div>';
            DOM.onlineTotal.textContent = '0 users online';
            return;
        }
        State.onlineUsers.forEach(u => {
            if (u.id === State.user?.id) return; // don't list yourself
            const div = document.createElement('div');
            div.className = 'user-item';
            const av = u.avatar_path
                ? `<img src="${fileUrl(u.avatar_path)}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:50%">`
                : `<span style="font-weight:600">${initialOf(displayNameOf(u))}</span>`;
            div.innerHTML = `
                <div class="user-avatar">${av}</div>
                <div class="user-info">
                    <div class="user-name">${escapeHtml(displayNameOf(u))}</div>
                    <div class="user-status-text"><span class="status-dot online"></span> Online</div>
                </div>`;
            DOM.onlineUsersList.appendChild(div);
        });
        DOM.onlineTotal.textContent = `${State.onlineUsers.size - (State.onlineUsers.has(State.user?.id) ? 1 : 0)} user${State.onlineUsers.size !== 1 ? 's' : ''} online`;
    }

    // ============================================================
    // Settings
    // ============================================================
    function openSettings() {
        DOM.settingsDisplayName.value = State.user?.display_name || '';
        DOM.settingsUsername.value = State.user?.username || '';
        DOM.pwCurrent.value = '';
        DOM.pwNew.value = '';
        DOM.pwMsg.textContent = '';

        if (State.user?.avatar_path) {
            DOM.settingsAvatarImg.src = fileUrl(State.user.avatar_path);
            DOM.settingsAvatarImg.classList.remove('hidden');
            DOM.settingsAvatarPreview.classList.add('hidden');
        } else {
            DOM.settingsAvatarImg.classList.add('hidden');
            DOM.settingsAvatarPreview.classList.remove('hidden');
        }

        DOM.themeOptions.forEach(opt => opt.classList.toggle('active', opt.dataset.theme === State.theme));
        DOM.customThemeSettings.classList.toggle('hidden', State.theme !== 'custom');
        if (State.theme === 'custom') {
            DOM.customSelfColor.value = State.customColors.self;
            DOM.customOtherColor.value = State.customColors.other;
            DOM.customTextColor.value = State.customColors.text;
            DOM.selfColorHex.textContent = State.customColors.self;
            DOM.otherColorHex.textContent = State.customColors.other;
            DOM.textColorHex.textContent = State.customColors.text;
        }
        DOM.aboutVersion.textContent = APP_VERSION;
        applyPrefs();
        openModal(DOM.settingsModal);
    }

    DOM.settingsBtn.addEventListener('click', openSettings);
    DOM.meSettingsBtn.addEventListener('click', openSettings);

    async function saveSettings() {
        const newName = DOM.settingsDisplayName.value.trim();
        if (newName && newName !== State.user?.display_name) {
            try {
                const res = await fetch(`${API}/api/profile?display_name=${encodeURIComponent(newName)}`, {
                    method: 'PATCH', headers: { 'X-Auth-Token': State.token }
                });
                if (res.ok) {
                    State.user.display_name = newName;
                    updateSidebarMe();
                    renderSidebar();
                    renderConversationHeader(State.activeConvId);
                    showToast('Display name updated', 'success');
                } else {
                    const data = await res.json().catch(() => ({}));
                    throw new Error(data.detail || 'Failed');
                }
            } catch (e) { showToast('Failed to update name', 'error'); }
        }

        const avatarFile = DOM.avatarInput.files[0];
        if (avatarFile) {
            const formData = new FormData();
            formData.append('file', avatarFile);
            DOM.avatarUploadBtn.disabled = true;
            DOM.avatarUploadBtn.innerHTML = '<div class="spinner spinner-sm"></div>';
            try {
                const res = await fetch(`${API}/api/profile/avatar`, {
                    method: 'POST', body: formData, headers: { 'X-Auth-Token': State.token }
                });
                if (res.ok) {
                    const data = await res.json();
                    State.user.avatar_path = data.avatar_path;
                    DOM.settingsAvatarImg.src = fileUrl(data.avatar_path);
                    DOM.settingsAvatarImg.classList.remove('hidden');
                    DOM.settingsAvatarPreview.classList.add('hidden');
                    updateSidebarMe();
                    renderSidebar();
                    renderConversationHeader(State.activeConvId);
                    showToast('Avatar updated', 'success');
                } else {
                    const data = await res.json().catch(() => ({}));
                    throw new Error(data.detail || 'Upload failed');
                }
            } catch (e) {
                showToast(e.message, 'error');
            } finally {
                DOM.avatarUploadBtn.disabled = false;
                DOM.avatarUploadBtn.innerHTML = '<i class="fas fa-camera"></i> Change Photo';
            }
        }

        State.prefs.autoScroll = DOM.toggleAutoScroll.checked;
        State.prefs.showScrollBtn = DOM.toggleScrollBtn.checked;
        State.prefs.showLatestBar = DOM.toggleLatestBar.checked;
        State.prefs.sound = DOM.toggleSound.checked;
        State.prefs.enterSend = DOM.toggleEnterSend.checked;
        savePrefs();

        const selectedTheme = document.querySelector('.theme-option.active')?.dataset.theme || 'light';
        if (selectedTheme === 'custom') {
            State.customColors.self = DOM.customSelfColor.value;
            State.customColors.other = DOM.customOtherColor.value;
            State.customColors.text = DOM.customTextColor.value;
            store.set('custom-self', State.customColors.self);
            store.set('custom-other', State.customColors.other);
            store.set('custom-text', State.customColors.text);
        }
        applyTheme(selectedTheme);
        closeModal(DOM.settingsModal);
    }

    DOM.customSelfColor.addEventListener('input', () => { DOM.selfColorHex.textContent = DOM.customSelfColor.value; });
    DOM.customOtherColor.addEventListener('input', () => { DOM.otherColorHex.textContent = DOM.customOtherColor.value; });
    DOM.customTextColor.addEventListener('input', () => { DOM.textColorHex.textContent = DOM.customTextColor.value; });

    DOM.themeOptions.forEach(opt => {
        opt.addEventListener('click', () => {
            DOM.themeOptions.forEach(o => o.classList.remove('active'));
            opt.classList.add('active');
            DOM.customThemeSettings.classList.toggle('hidden', opt.dataset.theme !== 'custom');
            if (opt.dataset.theme === 'custom') {
                DOM.customSelfColor.value = State.customColors.self;
                DOM.customOtherColor.value = State.customColors.other;
                DOM.customTextColor.value = State.customColors.text;
                DOM.selfColorHex.textContent = State.customColors.self;
                DOM.otherColorHex.textContent = State.customColors.other;
                DOM.textColorHex.textContent = State.customColors.text;
            }
        });
    });

    DOM.avatarUploadBtn.addEventListener('click', () => DOM.avatarInput.click());
    DOM.avatarInput.addEventListener('change', () => {
        const file = DOM.avatarInput.files[0];
        if (file) {
            const reader = new FileReader();
            reader.onload = (e) => {
                DOM.settingsAvatarImg.src = e.target.result;
                DOM.settingsAvatarImg.classList.remove('hidden');
                DOM.settingsAvatarPreview.classList.add('hidden');
            };
            reader.readAsDataURL(file);
        }
    });

    document.getElementById('save-settings').addEventListener('click', saveSettings);

    DOM.pwChangeBtn.addEventListener('click', async () => {
        const current = DOM.pwCurrent.value;
        const next = DOM.pwNew.value;
        if (!current || next.length < 6) {
            DOM.pwMsg.textContent = 'Enter your current password and a new one (6+ characters)';
            DOM.pwMsg.className = 'form-hint error';
            return;
        }
        DOM.pwChangeBtn.disabled = true;
        DOM.pwChangeBtn.innerHTML = '<div class="spinner spinner-sm"></div>';
        try {
            const res = await fetch(
                `${API}/api/profile/password?current_password=${encodeURIComponent(current)}&new_password=${encodeURIComponent(next)}`,
                { method: 'POST', headers: { 'X-Auth-Token': State.token } }
            );
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || 'Failed');
            DOM.pwMsg.textContent = 'Password updated successfully';
            DOM.pwMsg.className = 'form-hint success';
            DOM.pwCurrent.value = '';
            DOM.pwNew.value = '';
        } catch (err) {
            DOM.pwMsg.textContent = err.message;
            DOM.pwMsg.className = 'form-hint error';
        } finally {
            DOM.pwChangeBtn.disabled = false;
            DOM.pwChangeBtn.innerHTML = '<i class="fas fa-key"></i> Change Password';
        }
    });

    // ============================================================
    // Auth lifecycle
    // ============================================================
    async function init() {
        applyPrefs();
        DOM.aboutVersion.textContent = APP_VERSION;
        if (State.token) {
            try {
                const res = await fetch(`${API}/api/auth/verify`, { headers: { 'X-Auth-Token': State.token } });
                if (res.ok) {
                    const data = await res.json();
                    State.user = data.user;
                    showChat();
                    connectWebSocket();
                    return;
                }
            } catch (e) { /* fall through to auth screen */ }
            store.remove('token');
            State.token = null;
        }
        showAuth();
    }

    function showAuth() {
        State.wsState = 'disconnected';
        DOM.authScreen.classList.add('active');
        DOM.chatScreen.classList.remove('active');
    }

    function showChat() {
        DOM.authScreen.classList.remove('active');
        DOM.chatScreen.classList.add('active');
        updateSidebarMe();
    }

    function updateSidebarMe() {
        if (!State.user) return;
        DOM.sbMeName.textContent = displayNameOf(State.user);
        if (State.user.avatar_path) {
            DOM.sbMeAvatar.src = fileUrl(State.user.avatar_path);
            DOM.sbMeAvatar.classList.remove('hidden');
            DOM.sbMeAvatarPh.classList.add('hidden');
        } else {
            DOM.sbMeAvatar.classList.add('hidden');
            DOM.sbMeAvatarPh.classList.remove('hidden');
        }
    }

    function forceLogout() {
        showToast('Session expired - please sign in again', 'warning');
        store.remove('token');
        State.token = null;
        State.wsState = 'disconnected';
        if (State.ws) { try { State.ws.close(); } catch (e) { /* noop */ } }
        State.ws = null;
        resetAllState();
        showAuth();
    }

    function resetAllState() {
        State.user = null;
        State.conversations.clear();
        State.conversationOrder = [];
        State.convCache.clear();
        State.messages.clear();
        State.tombstoned.clear();
        State.pendingSends.clear();
        State.onlineUsers.clear();
        State.typingUsers.clear();
        State.activeConvId = null;
        State.cursor = null;
        State.replyTo = null;
        State.pendingFile = null;
        State.latestUnseen = null;
        State.editingId = null;
        DOM.messagesList.innerHTML = '';
        DOM.convList.innerHTML = '';
        DOM.sidebar.classList.remove('open');
        DOM.sidebarBackdrop.classList.add('hidden');
        DOM.loadOlderBtn.classList.add('hidden');
        DOM.emptyState.style.display = 'flex';
        hideLatestBar();
        updateOnlineBadge();
        updateTitleUnread();
        clearReply();
        clearPendingFile();
        DOM.messageInput.value = '';
        DOM.sendBtn.disabled = true;
        DOM.uploadProgress.classList.add('hidden');
        DOM.onlineModal.classList.add('hidden');
        DOM.settingsModal.classList.add('hidden');
        DOM.convModal.classList.add('hidden');
        DOM.receiptsModal.classList.add('hidden');
        DOM.confirmModal.classList.add('hidden');
        DOM.lightboxModal.classList.add('hidden');
        DOM.emojiPicker.classList.add('hidden');
        openModals.length = 0;
        document.body.style.overflow = '';
    }

    function doLogout() {
        if (State.token) {
            fetch(`${API}/api/auth/logout`, { method: 'POST', headers: { 'X-Auth-Token': State.token } }).catch(() => { });
        }
        if (State.ws) { try { State.ws.close(); } catch (e) { /* noop */ } }
        State.ws = null;
        store.remove('token');
        State.token = null;
        resetAllState();
        showAuth();
    }

    DOM.logoutBtn.addEventListener('click', () => {
        showConfirm({
            title: 'Log out',
            message: 'Log out of InfinityChat on this device?',
            okText: 'Log Out',
            danger: false
        }).then(ok => { if (ok) doLogout(); });
    });
    DOM.sidebarLogoutBtn.addEventListener('click', () => {
        showConfirm({
            title: 'Log out',
            message: 'Log out of InfinityChat on this device?',
            okText: 'Log Out',
            danger: false
        }).then(ok => { if (ok) doLogout(); });
    });

    document.querySelectorAll('.input-toggle').forEach(btn => {
        btn.addEventListener('click', () => {
            const input = document.getElementById(btn.dataset.toggle);
            if (input) {
                input.type = input.type === 'password' ? 'text' : 'password';
                const icon = btn.querySelector('i');
                if (icon) icon.className = input.type === 'password' ? 'fas fa-eye' : 'fas fa-eye-slash';
            }
        });
    });

    // ============================================================
    // Go
    // ============================================================
    init();
})();
