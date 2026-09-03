// static/script.js
(function() {
    'use strict';

    // =============================================
    // State
    // =============================================
    const State = {
        token: localStorage.getItem('token'),
        user: null,
        ws: null,
        wsState: 'disconnected',
        messages: new Map(),
        messageOrder: [],
        cursor: null,
        hasMore: true,
        replyTo: null,
        pendingFile: null,
        typingUsers: new Map(),
        onlineUsers: new Map(),
        isAtBottom: true,
        theme: localStorage.getItem('theme') || 'light',
        customColors: {
            self: localStorage.getItem('custom-self') || '#2563EB',
            other: localStorage.getItem('custom-other') || '#F3F4F6',
            text: localStorage.getItem('custom-text') || '#111827'
        },
        prefs: {
            autoScroll: localStorage.getItem('pref-autoscroll') !== 'false',
            showScrollBtn: localStorage.getItem('pref-scrollbtn') !== 'false',
            showLatestBar: localStorage.getItem('pref-latestbar') !== 'false'
        },
        latestUnseen: null
    };

    // =============================================
    // DOM Cache
    // =============================================
    const DOM = {
        authScreen: document.getElementById('auth-screen'),
        chatScreen: document.getElementById('chat-screen'),
        loginForm: document.getElementById('login-form'),
        signupForm: document.getElementById('signup-form'),
        authTabs: document.querySelectorAll('.auth-tab'),
        authError: document.getElementById('auth-error'),
        authErrorText: document.getElementById('auth-error-text'),
        authLoading: document.getElementById('auth-loading'),
        headerDisplayName: document.getElementById('header-display-name'),
        headerAvatar: document.getElementById('header-avatar'),
        headerAvatarPlaceholder: document.getElementById('header-avatar-placeholder'),
        headerStatus: document.getElementById('header-status'),
        onlineBadge: document.getElementById('online-badge'),
        connectionStatus: document.getElementById('connection-status'),
        connectionText: document.getElementById('connection-text'),
        messagesList: document.getElementById('messages-list'),
        messagesScroll: document.getElementById('messages-scroll'),
        emptyState: document.getElementById('empty-state'),
        loadOlderBtn: document.getElementById('load-older-btn'),
        scrollBottomBtn: document.getElementById('scroll-bottom-btn'),
        latestMessageBar: document.getElementById('latest-message-bar'),
        lmbAvatar: document.getElementById('lmb-avatar'),
        lmbName: document.getElementById('lmb-name'),
        lmbText: document.getElementById('lmb-text'),
        typingIndicator: document.getElementById('typing-indicator'),
        typingText: document.getElementById('typing-text'),
        replyPreview: document.getElementById('reply-preview'),
        replyUser: document.getElementById('reply-user'),
        replyText: document.getElementById('reply-text'),
        composeForm: document.getElementById('compose-form'),
        messageInput: document.getElementById('message-input'),
        sendBtn: document.getElementById('send-btn'),
        attachBtn: document.getElementById('attach-btn'),
        emojiBtn: document.getElementById('emoji-btn'),
        fileInput: document.getElementById('file-input'),
        uploadPreview: document.getElementById('upload-preview'),
        uploadFilename: document.getElementById('upload-filename'),
        cancelUpload: document.getElementById('cancel-upload'),
        uploadProgress: document.getElementById('upload-progress'),
        progressFill: document.getElementById('progress-fill'),
        progressText: document.getElementById('progress-text'),
        settingsModal: document.getElementById('settings-modal'),
        onlineModal: document.getElementById('online-modal'),
        emojiPicker: document.getElementById('emoji-picker'),
        emojiGrid: document.getElementById('emoji-grid'),
        settingsDisplayName: document.getElementById('settings-display-name'),
        settingsUsername: document.getElementById('settings-username'),
        settingsAvatarPreview: document.getElementById('settings-avatar-preview'),
        settingsAvatarImg: document.getElementById('settings-avatar-img'),
        avatarInput: document.getElementById('avatar-input'),
        avatarUploadBtn: document.getElementById('avatar-upload-btn'),
        themeOptions: document.querySelectorAll('.theme-option'),
        customThemeSettings: document.getElementById('custom-theme-settings'),
        customSelfColor: document.getElementById('custom-self-color'),
        customOtherColor: document.getElementById('custom-other-color'),
        customTextColor: document.getElementById('custom-text-color'),
        selfColorHex: document.getElementById('self-color-hex'),
        otherColorHex: document.getElementById('other-color-hex'),
        textColorHex: document.getElementById('text-color-hex'),
        toggleAutoScroll: document.getElementById('toggle-autoscroll'),
        toggleScrollBtn: document.getElementById('toggle-scroll-btn'),
        toggleLatestBar: document.getElementById('toggle-latest-bar'),
        onlineBtn: document.getElementById('online-btn'),
        onlineUsersList: document.getElementById('online-users-list'),
        onlineTotal: document.getElementById('online-total'),
        toastContainer: document.getElementById('toast-container')
    };

    const API = window.location.origin;

    // =============================================
    // File URL helper
    // =============================================
    function fileUrl(filePath) {
        return `${API}/api/download/${encodeURIComponent(filePath)}?t=${encodeURIComponent(State.token)}`;
    }

    // =============================================
    // Theme
    // =============================================
    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        State.theme = theme;
        localStorage.setItem('theme', theme);
        if (theme === 'custom') {
            const s = State.customColors.self;
            const o = State.customColors.other;
            const t = State.customColors.text;
            document.documentElement.style.setProperty('--bubble-self', s);
            document.documentElement.style.setProperty('--bubble-other', o);
            document.documentElement.style.setProperty('--bubble-other-text', t);
            const isLight = (c) => {
                const r = parseInt(c.slice(1,3),16), g = parseInt(c.slice(3,5),16), b = parseInt(c.slice(5,7),16);
                return (0.299*r + 0.587*g + 0.114*b) > 150;
            };
            document.documentElement.style.setProperty('--bubble-self-text', isLight(s) ? '#111827' : '#FFFFFF');
        } else {
            document.documentElement.style.removeProperty('--bubble-self');
            document.documentElement.style.removeProperty('--bubble-other');
            document.documentElement.style.removeProperty('--bubble-other-text');
            document.documentElement.style.removeProperty('--bubble-self-text');
        }
    }

    applyTheme(State.theme);

    // =============================================
    // Preferences
    // =============================================
    function applyPrefs() {
        if (DOM.toggleAutoScroll) DOM.toggleAutoScroll.checked = State.prefs.autoScroll;
        if (DOM.toggleScrollBtn) DOM.toggleScrollBtn.checked = State.prefs.showScrollBtn;
        if (DOM.toggleLatestBar) DOM.toggleLatestBar.checked = State.prefs.showLatestBar;
        updateScrollUI();
    }

    function savePrefs() {
        localStorage.setItem('pref-autoscroll', State.prefs.autoScroll);
        localStorage.setItem('pref-scrollbtn', State.prefs.showScrollBtn);
        localStorage.setItem('pref-latestbar', State.prefs.showLatestBar);
    }

    if (DOM.toggleAutoScroll) {
        DOM.toggleAutoScroll.addEventListener('change', () => {
            State.prefs.autoScroll = DOM.toggleAutoScroll.checked;
            savePrefs();
            if (State.prefs.autoScroll) {
                scrollToBottom();
                hideLatestBar();
            }
        });
    }
    if (DOM.toggleScrollBtn) {
        DOM.toggleScrollBtn.addEventListener('change', () => {
            State.prefs.showScrollBtn = DOM.toggleScrollBtn.checked;
            savePrefs();
            updateScrollUI();
        });
    }
    if (DOM.toggleLatestBar) {
        DOM.toggleLatestBar.addEventListener('change', () => {
            State.prefs.showLatestBar = DOM.toggleLatestBar.checked;
            savePrefs();
            if (!State.prefs.showLatestBar) hideLatestBar();
        });
    }

    // =============================================
    // Timestamp updater
    // =============================================
    function startTimestampUpdater() {
        setInterval(() => {
            document.querySelectorAll('.message-time[data-ts]').forEach(el => {
                el.textContent = formatTime(parseInt(el.dataset.ts));
            });
        }, 30000);
    }

    // =============================================
    // Toast
    // =============================================
    function showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        const icons = { success:'check-circle', error:'exclamation-circle', warning:'exclamation-triangle', info:'info-circle' };
        toast.innerHTML = `<i class="fas fa-${icons[type]||'info-circle'}"></i><span>${message}</span>`;
        DOM.toastContainer.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    }

    // =============================================
    // Auth
    // =============================================
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
        await login(
            document.getElementById('login-username').value.trim(),
            document.getElementById('login-password').value
        );
    });

    DOM.signupForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const username = document.getElementById('signup-username').value.trim();
        const display = document.getElementById('signup-display').value.trim() || username;
        const password = document.getElementById('signup-password').value;
        await signup(username, password, display);
    });

    async function login(username, password) {
        DOM.authLoading.classList.remove('hidden');
        hideAuthError();
        try {
            const res = await fetch(`${API}/api/auth/login?username=${encodeURIComponent(username)}&password=${encodeURIComponent(password)}`, { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Login failed');
            handleAuthSuccess(data);
        } catch (err) {
            showAuthError(err.message);
        } finally {
            DOM.authLoading.classList.add('hidden');
        }
    }

    async function signup(username, password, display) {
        DOM.authLoading.classList.remove('hidden');
        hideAuthError();
        try {
            const res = await fetch(`${API}/api/auth/signup?username=${encodeURIComponent(username)}&password=${encodeURIComponent(password)}&display_name=${encodeURIComponent(display)}`, { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Signup failed');
            handleAuthSuccess(data);
        } catch (err) {
            showAuthError(err.message);
        } finally {
            DOM.authLoading.classList.add('hidden');
        }
    }

    function handleAuthSuccess(data) {
        State.token = data.token;
        State.user = data.user;
        localStorage.setItem('token', State.token);
        showChat();
        connectWebSocket();
    }

    function showAuthError(msg) {
        DOM.authErrorText.textContent = msg;
        DOM.authError.style.display = 'flex';
    }

    function hideAuthError() {
        DOM.authError.style.display = 'none';
    }

    // =============================================
    // WebSocket
    // =============================================
    function connectWebSocket() {
        if (!State.token) return;
        if (State.ws && State.ws.readyState === WebSocket.OPEN) return;
        State.wsState = 'connecting';
        updateConnectionUI();
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        State.ws = new WebSocket(`${proto}//${location.host}/ws?token=${encodeURIComponent(State.token)}`);
        State.ws.onopen = () => {
            State.wsState = 'connected';
            updateConnectionUI();
            loadMessages();
            requestOnlineUsers();
        };
        State.ws.onmessage = (e) => {
            try { handleWSMessage(JSON.parse(e.data)); } catch(err) { console.error('WS parse error', err); }
        };
        State.ws.onclose = () => {
            if (State.wsState === 'connected') {
                State.wsState = 'reconnecting';
                updateConnectionUI();
                setTimeout(connectWebSocket, 3000);
            } else {
                State.wsState = 'disconnected';
                updateConnectionUI();
            }
        };
        State.ws.onerror = () => {
            State.wsState = 'disconnected';
            updateConnectionUI();
        };
    }

    function updateConnectionUI() {
        DOM.connectionStatus.className = 'connection-status';
        const map = {
            connected: ['connected', 'Connected'],
            reconnecting: ['reconnecting', 'Reconnecting...'],
            disconnected: ['disconnected', 'Disconnected'],
            connecting: ['reconnecting', 'Connecting...']
        };
        const [cls, txt] = map[State.wsState] || ['disconnected', 'Disconnected'];
        DOM.connectionStatus.classList.add(cls);
        DOM.connectionText.textContent = txt;
    }

    function handleWSMessage(msg) {
        switch (msg.type) {
            case 'connection_established':
                State.user = {
                    id: msg.user_id,
                    username: msg.username,
                    display_name: msg.display_name,
                    avatar_path: msg.avatar_path
                };
                State.onlineUsers.clear();
                msg.online_users.forEach(u => State.onlineUsers.set(u.id, u));
                updateHeader();
                updateOnlineBadge();
                break;

            case 'new_message': {
                const m = msg.message;
                if (State.messages.has(m.id)) {
                    const ex = State.messages.get(m.id);
                    ex.status = m.status;
                    updateMessageBubble(m.id);
                    return;
                }
                State.messages.set(m.id, m);
                insertMessageIdSorted(m.id);
                renderMessage(m, true);

                if (State.isAtBottom) {
                    if (State.prefs.autoScroll) scrollToBottom();
                } else {
                    // Show latest message bar if not at bottom
                    State.latestUnseen = m;
                    showLatestBar(m);
                }

                if (document.visibilityState === 'visible' && State.isAtBottom) {
                    sendMarkRead(m.id);
                }
                if (document.visibilityState !== 'visible' && Notification.permission === 'granted') {
                    new Notification(m.sender_display_name || m.sender_username, {
                        body: m.content || 'Sent a file',
                        icon: '/static/icon.png'
                    });
                }
                break;
            }

            case 'messages_loaded': {
                const sorted = [...msg.messages].sort((a, b) => a.id - b.id);
                sorted.forEach(m => {
                    if (!State.messages.has(m.id)) {
                        State.messages.set(m.id, m);
                        insertMessageIdSorted(m.id);
                    }
                });
                State.hasMore = msg.has_more;
                if (msg.next_cursor) State.cursor = msg.next_cursor;
                renderAllMessages();
                DOM.loadOlderBtn.classList.toggle('hidden', !msg.has_more);
                break;
            }

            case 'user_status':
                if (msg.status === 'online') {
                    State.onlineUsers.set(msg.user_id, {
                        id: msg.user_id,
                        username: msg.username,
                        display_name: msg.display_name,
                        avatar_path: msg.avatar_path
                    });
                } else {
                    State.onlineUsers.delete(msg.user_id);
                }
                updateOnlineBadge();
                break;

            case 'typing_indicator':
                if (msg.is_typing) State.typingUsers.set(msg.user_id, msg);
                else State.typingUsers.delete(msg.user_id);
                updateTypingIndicator();
                break;

            case 'message_edited':
                if (State.messages.has(msg.message_id)) {
                    const m = State.messages.get(msg.message_id);
                    m.content = msg.content;
                    m.is_edited = true;
                    updateMessageBubble(msg.message_id);
                }
                break;

            case 'message_deleted':
                if (State.messages.has(msg.message_id)) {
                    State.messages.get(msg.message_id).is_deleted = true;
                    updateMessageBubble(msg.message_id);
                }
                break;

            case 'message_read':
                if (State.messages.has(msg.message_id)) {
                    const m = State.messages.get(msg.message_id);
                    if (m.sender_id === State.user.id) {
                        m.status = 'read';
                        updateMessageBubble(msg.message_id);
                    }
                }
                break;

            case 'online_users':
                State.onlineUsers.clear();
                msg.users.forEach(u => State.onlineUsers.set(u.id, u));
                updateOnlineBadge();
                if (!DOM.onlineModal.classList.contains('hidden')) renderOnlineUsers();
                break;

            case 'error':
                showToast(msg.message, 'error');
                break;
        }
    }

    function insertMessageIdSorted(id) {
        let lo = 0, hi = State.messageOrder.length;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (State.messageOrder[mid] < id) lo = mid + 1;
            else hi = mid;
        }
        if (State.messageOrder[lo] !== id) {
            State.messageOrder.splice(lo, 0, id);
        }
    }

    // =============================================
    // Latest message bar
    // =============================================
    function showLatestBar(msg) {
        if (!State.prefs.showLatestBar || !DOM.latestMessageBar) return;
        const name = msg.sender_display_name || msg.sender_username || 'Unknown';
        const text = msg.content || (msg.file_name ? '📎 ' + msg.file_name : '📎 File');

        // Avatar
        if (msg.sender_avatar_path) {
            DOM.lmbAvatar.innerHTML = `<img src="${fileUrl(msg.sender_avatar_path)}" alt="${escapeHtml(name)}">`;
        } else {
            DOM.lmbAvatar.textContent = name.charAt(0).toUpperCase();
            DOM.lmbAvatar.style.background = 'var(--primary)';
        }

        DOM.lmbName.textContent = name + ':';
        DOM.lmbText.textContent = truncate(text, 40);
        DOM.latestMessageBar.classList.remove('hidden');
    }

    function hideLatestBar() {
        if (DOM.latestMessageBar) DOM.latestMessageBar.classList.add('hidden');
        State.latestUnseen = null;
    }

    if (DOM.latestMessageBar) {
        DOM.latestMessageBar.addEventListener('click', () => {
            scrollToBottom();
            hideLatestBar();
        });
    }

    // =============================================
    // Scroll UI
    // =============================================
    function updateScrollUI() {
        if (!State.isAtBottom) {
            if (State.prefs.showScrollBtn) {
                DOM.scrollBottomBtn.classList.remove('hidden');
            } else {
                DOM.scrollBottomBtn.classList.add('hidden');
            }
        } else {
            DOM.scrollBottomBtn.classList.add('hidden');
            hideLatestBar();
        }
    }

    DOM.messagesScroll.addEventListener('scroll', () => {
        const { scrollTop, scrollHeight, clientHeight } = DOM.messagesScroll;
        State.isAtBottom = (scrollHeight - scrollTop - clientHeight) < 80;
        updateScrollUI();
        if (State.isAtBottom) {
            hideLatestBar();
            if (State.latestUnseen) sendMarkRead(State.latestUnseen.id);
        }
    });

    DOM.scrollBottomBtn.addEventListener('click', () => {
        scrollToBottom();
        hideLatestBar();
    });

    function scrollToBottom() {
        DOM.messagesScroll.scrollTop = DOM.messagesScroll.scrollHeight;
        State.isAtBottom = true;
        DOM.scrollBottomBtn.classList.add('hidden');
    }

    // =============================================
    // Message Actions
    // =============================================
    function loadMessages() {
        if (State.ws?.readyState === WebSocket.OPEN) {
            State.ws.send(JSON.stringify({ type: 'load_messages', limit: 50 }));
        }
    }

    function requestOnlineUsers() {
        if (State.ws?.readyState === WebSocket.OPEN) {
            State.ws.send(JSON.stringify({ type: 'get_online_users' }));
        }
    }

    function sendMarkRead(messageId) {
        if (State.ws?.readyState === WebSocket.OPEN) {
            State.ws.send(JSON.stringify({ type: 'mark_read', up_to_message_id: messageId }));
        }
    }

    // =============================================
    // Rendering
    // =============================================
    function buildSenderAvatarHTML(msg) {
        const name = escapeHtml(msg.sender_display_name || msg.sender_username || '?');
        const initial = (msg.sender_display_name || msg.sender_username || '?').charAt(0).toUpperCase();
        if (msg.sender_avatar_path) {
            return `<img class="msg-avatar" src="${fileUrl(msg.sender_avatar_path)}" alt="${name}" title="${name}">`;
        }
        return `<div class="msg-avatar-placeholder" title="${name}">${initial}</div>`;
    }

    function renderMessage(msg, append = false) {
        if (DOM.messagesList.querySelector(`[data-id="${msg.id}"]`)) return;
        const wrapper = createMessageElement(msg);
        if (append) {
            DOM.messagesList.appendChild(wrapper);
        } else {
            const ids = State.messageOrder;
            const idx = ids.indexOf(msg.id);
            const nextId = ids[idx + 1];
            if (nextId) {
                const nextEl = DOM.messagesList.querySelector(`[data-id="${nextId}"]`);
                if (nextEl) { DOM.messagesList.insertBefore(wrapper, nextEl); return; }
            }
            DOM.messagesList.appendChild(wrapper);
        }
    }

    function createMessageElement(msg) {
        const isSelf = msg.sender_id === State.user?.id;
        const wrapper = document.createElement('div');
        wrapper.className = `message-wrapper ${isSelf ? 'self' : 'other'}`;
        wrapper.dataset.id = msg.id;
        wrapper.innerHTML = buildMessageHTML(msg);
        return wrapper;
    }

    function buildMessageHTML(msg) {
        if (msg.is_deleted) {
            return '<div class="message-bubble deleted"><i class="fas fa-ban"></i> This message was deleted</div>';
        }

        const isSelf = msg.sender_id === State.user?.id;
        const displayName = escapeHtml(msg.sender_display_name || msg.sender_username || 'Unknown');
        let html = '';

        // Sender row: avatar + name, always shown
        if (!isSelf) {
            html += `<div class="msg-sender-row">
                ${buildSenderAvatarHTML(msg)}
                <span class="msg-sender-name">${displayName}</span>
            </div>`;
        } else {
            html += `<div class="msg-sender-row msg-sender-row-self">
                <span class="msg-sender-name">${displayName}</span>
                ${buildSenderAvatarHTML(msg)}
            </div>`;
        }

        // Reply indicator
        if (msg.reply_to_id && State.messages.has(msg.reply_to_id)) {
            const reply = State.messages.get(msg.reply_to_id);
            const rName = escapeHtml(reply.sender_display_name || reply.sender_username || 'Unknown');
            const rText = escapeHtml(truncate(reply.content || (reply.file_name ? '📎 ' + reply.file_name : ''), 60));
            html += `<div class="reply-indicator">
                <i class="fas fa-reply"></i>
                <span><strong>${rName}:</strong> ${rText}</span>
            </div>`;
        }

        html += `<div class="message-bubble">`;

        // Text content
        if (msg.content) {
            html += `<div class="message-text">${escapeHtml(msg.content)}</div>`;
        }

        // Attachment
        if (msg.file_path) {
            html += buildAttachmentHTML(msg);
        }

        // Meta
        html += `<div class="message-meta">
            <span class="message-time" data-ts="${msg.timestamp_ms}">${formatTime(msg.timestamp_ms)}</span>`;
        if (msg.is_edited) html += ` <span class="edited-label">edited</span>`;
        if (isSelf) {
            const isRead = msg.status === 'read';
            html += `<span class="message-status${isRead ? ' read' : ''}">
                <i class="fas fa-check${isRead ? '-double' : ''}"></i>
            </span>`;
        }
        html += `</div>`;

        // Actions - always visible
        html += `<div class="message-actions">
            <button onclick="InfinityChat.replyTo(${msg.id})" title="Reply"><i class="fas fa-reply"></i></button>`;
        if (isSelf) {
            html += `<button onclick="InfinityChat.editMessage(${msg.id})" title="Edit"><i class="fas fa-pen"></i></button>
                     <button onclick="InfinityChat.deleteMessage(${msg.id})" title="Delete"><i class="fas fa-trash"></i></button>`;
        }
        html += `</div>`;

        html += `</div>`; // close message-bubble

        return html;
    }

    function buildAttachmentHTML(msg) {
        const url = fileUrl(msg.file_path);
        const ft = (msg.file_type || '').toLowerCase();
        const fname = escapeHtml(msg.file_name || 'File');

        if (ft.startsWith('image/')) {
            return `<div class="attachment">
                <img src="${url}"
                     alt="${fname}"
                     loading="lazy"
                     onclick="window.open('${url}', '_blank')"
                     onerror="this.parentElement.innerHTML='<div class=\\'attachment-file\\' onclick=\\'window.open(\\'${url}\\',\\'_blank\\')\\'>'+
                         '<i class=\\'fas fa-image\\'></i>'+
                         '<div class=\\'file-info\\'>'+
                         '<div class=\\'file-name\\'>${fname}</div>'+
                         '<div class=\\'file-size\\'>${formatFileSize(msg.file_size)}</div>'+
                         '</div></div>'">
            </div>`;
        }
        if (ft.startsWith('video/')) {
            return `<div class="attachment">
                <video controls preload="metadata">
                    <source src="${url}" type="${escapeHtml(msg.file_type)}">
                </video>
            </div>`;
        }
        if (ft.startsWith('audio/')) {
            return `<div class="attachment"><audio controls src="${url}"></audio></div>`;
        }
        return `<div class="attachment-file" onclick="window.open('${url}', '_blank')">
            <i class="fas fa-file-alt"></i>
            <div class="file-info">
                <div class="file-name">${fname}</div>
                <div class="file-size">${formatFileSize(msg.file_size)}</div>
            </div>
        </div>`;
    }

    function updateMessageBubble(id) {
        const el = DOM.messagesList.querySelector(`[data-id="${id}"]`);
        if (!el) return;
        const msg = State.messages.get(id);
        if (!msg) return;
        const isSelf = msg.sender_id === State.user?.id;
        el.className = `message-wrapper ${isSelf ? 'self' : 'other'}`;
        el.innerHTML = buildMessageHTML(msg);
    }

    function renderAllMessages() {
        const wasAtBottom = State.isAtBottom;
        const prevScrollHeight = DOM.messagesScroll.scrollHeight;

        DOM.messagesList.innerHTML = '';

        if (State.messageOrder.length === 0) {
            DOM.emptyState.style.display = 'flex';
            return;
        }
        DOM.emptyState.style.display = 'none';

        State.messageOrder.forEach(id => {
            const msg = State.messages.get(id);
            if (msg) renderMessage(msg, true);
        });

        if (wasAtBottom && State.prefs.autoScroll) {
            scrollToBottom();
        } else if (!wasAtBottom) {
            DOM.messagesScroll.scrollTop += (DOM.messagesScroll.scrollHeight - prevScrollHeight);
        } else {
            scrollToBottom();
        }
    }

    // =============================================
    // Compose
    // =============================================
    DOM.composeForm.addEventListener('submit', (e) => {
        e.preventDefault();
        sendMessage();
    });

    DOM.messageInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    DOM.messageInput.addEventListener('input', () => {
        const hasContent = DOM.messageInput.value.trim().length > 0 || !!State.pendingFile;
        DOM.sendBtn.disabled = !hasContent;
        if (hasContent) sendTyping(true);
        autoResize();
    });

    function sendMessage() {
        const content = DOM.messageInput.value.trim();
        if (!content && !State.pendingFile) return;
        if (!State.ws || State.ws.readyState !== WebSocket.OPEN) {
            showToast('Not connected', 'warning');
            return;
        }
        const payload = {
            type: 'send_message',
            content: content,
            reply_to_id: State.replyTo ? State.replyTo.id : null,
            client_id: generateId()
        };
        if (State.pendingFile) {
            payload.file_path = State.pendingFile.file_path;
            payload.file_type = State.pendingFile.file_type;
            payload.file_name = State.pendingFile.file_name;
            payload.file_size = State.pendingFile.file_size;
        }
        State.ws.send(JSON.stringify(payload));
        DOM.messageInput.value = '';
        DOM.sendBtn.disabled = true;
        clearReply();
        clearPendingFile();
        autoResize();
        sendTyping(false);
    }

    function sendTyping(isTyping) {
        if (!State.ws || State.ws.readyState !== WebSocket.OPEN) return;
        clearTimeout(window.__typingTimer);
        State.ws.send(JSON.stringify({ type: 'typing', is_typing: isTyping }));
        if (isTyping) {
            window.__typingTimer = setTimeout(() => sendTyping(false), 2000);
        }
    }

    function updateTypingIndicator() {
        const users = Array.from(State.typingUsers.values()).filter(u => u.user_id !== State.user?.id);
        DOM.typingIndicator.classList.toggle('hidden', users.length === 0);
        if (users.length > 0) {
            DOM.typingText.textContent = users.map(u => u.display_name || u.username).join(', ') +
                (users.length === 1 ? ' is typing...' : ' are typing...');
        }
    }

    // =============================================
    // Reply
    // =============================================
    function replyTo(messageId) {
        const msg = State.messages.get(messageId);
        if (!msg) return;
        State.replyTo = msg;
        DOM.replyUser.textContent = msg.sender_display_name || msg.sender_username;
        DOM.replyText.textContent = truncate(msg.content || (msg.file_name ? '📎 ' + msg.file_name : ''), 60);
        DOM.replyPreview.classList.remove('hidden');
        DOM.messageInput.focus();
    }

    function clearReply() {
        State.replyTo = null;
        DOM.replyPreview.classList.add('hidden');
    }

    document.addEventListener('click', (e) => {
        if (e.target.id === 'cancel-reply' || e.target.closest('#cancel-reply')) clearReply();
    });

    // =============================================
    // Edit / Delete
    // =============================================
    function editMessage(messageId) {
        const msg = State.messages.get(messageId);
        if (!msg) return;
        const newContent = prompt('Edit message:', msg.content);
        if (newContent !== null && newContent.trim()) {
            State.ws?.send(JSON.stringify({ type: 'edit_message', message_id: messageId, content: newContent.trim() }));
        }
    }

    function deleteMessage(messageId) {
        if (confirm('Permanently delete this message?')) {
            State.ws?.send(JSON.stringify({ type: 'delete_message', message_id: messageId }));
        }
    }

    // =============================================
    // File Upload
    // =============================================
    DOM.attachBtn.addEventListener('click', () => DOM.fileInput.click());
    DOM.fileInput.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        await uploadChunked(file);
        e.target.value = '';
    });

    async function uploadChunked(file) {
        const CHUNK_SIZE = 5 * 1024 * 1024;
        const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
        const uploadId = generateId();
        DOM.uploadProgress.classList.remove('hidden');
        updateProgress(0, file.name);

        for (let i = 0; i < totalChunks; i++) {
            const blob = file.slice(i * CHUNK_SIZE, Math.min((i + 1) * CHUNK_SIZE, file.size));
            const formData = new FormData();
            formData.append('file', blob);
            const url = `${API}/api/upload/chunk?chunk_index=${i}&total_chunks=${totalChunks}` +
                `&file_name=${encodeURIComponent(file.name)}&file_type=${encodeURIComponent(file.type || 'application/octet-stream')}` +
                `&file_size=${file.size}&upload_id=${uploadId}`;
            try {
                const res = await fetch(url, { method: 'POST', body: formData, headers: { 'X-Auth-Token': State.token } });
                if (!res.ok) throw new Error('Upload failed');
                const data = await res.json();
                updateProgress(((i + 1) / totalChunks) * 100, file.name);
                if (data.status === 'complete') {
                    State.pendingFile = {
                        file_path: data.file_path,
                        file_type: data.file_type || file.type,
                        file_name: data.file_name,
                        file_size: data.file_size
                    };
                    showUploadPreview(data.file_name);
                    DOM.uploadProgress.classList.add('hidden');
                    DOM.sendBtn.disabled = false;
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

    function updateProgress(percent, filename) {
        DOM.progressFill.style.width = `${percent}%`;
        DOM.progressText.textContent = `${Math.round(percent)}%`;
        if (filename) DOM.uploadFilename.textContent = filename;
    }

    function showUploadPreview(filename) {
        DOM.uploadPreview.classList.remove('hidden');
        DOM.uploadFilename.textContent = filename;
    }

    function clearPendingFile() {
        State.pendingFile = null;
        DOM.uploadPreview.classList.add('hidden');
    }

    DOM.cancelUpload.addEventListener('click', () => {
        clearPendingFile();
        DOM.sendBtn.disabled = DOM.messageInput.value.trim().length === 0;
    });

    // =============================================
    // Emoji Picker
    // =============================================
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

    // =============================================
    // Load More
    // =============================================
    DOM.loadOlderBtn.addEventListener('click', () => {
        if (State.ws?.readyState === WebSocket.OPEN && State.hasMore && State.cursor) {
            State.isAtBottom = false;
            State.ws.send(JSON.stringify({ type: 'load_messages', cursor: State.cursor, limit: 50 }));
        }
    });

    // =============================================
    // Online Users
    // =============================================
    DOM.onlineBtn.addEventListener('click', () => {
        renderOnlineUsers();
        DOM.onlineModal.classList.remove('hidden');
    });

    document.querySelectorAll('[data-close="online-modal"]').forEach(btn => {
        btn.addEventListener('click', () => DOM.onlineModal.classList.add('hidden'));
    });

    function updateOnlineBadge() {
        DOM.onlineBadge.textContent = State.onlineUsers.size;
    }

    function renderOnlineUsers() {
        DOM.onlineUsersList.innerHTML = '';
        if (State.onlineUsers.size === 0) {
            DOM.onlineUsersList.innerHTML = '<div class="loading-state"><span>No users online</span></div>';
            DOM.onlineTotal.textContent = '0 users online';
            return;
        }
        State.onlineUsers.forEach(u => {
            const div = document.createElement('div');
            div.className = 'user-item';
            let avatarHtml;
            if (u.avatar_path) {
                avatarHtml = `<img src="${fileUrl(u.avatar_path)}" alt="${escapeHtml(u.display_name || u.username)}" style="width:100%;height:100%;object-fit:cover;border-radius:50%">`;
            } else {
                avatarHtml = (u.display_name || u.username || '?').charAt(0).toUpperCase();
            }
            div.innerHTML = `
                <div class="user-avatar">${avatarHtml}</div>
                <div class="user-info">
                    <div class="user-name">${escapeHtml(u.display_name || u.username)}</div>
                    <div class="user-status-text"><span style="color:var(--success)">●</span> Online</div>
                </div>`;
            DOM.onlineUsersList.appendChild(div);
        });
        DOM.onlineTotal.textContent = `${State.onlineUsers.size} user${State.onlineUsers.size !== 1 ? 's' : ''} online`;
    }

    // =============================================
    // Settings
    // =============================================
    document.getElementById('settings-btn').addEventListener('click', openSettings);
    document.querySelectorAll('[data-close="settings-modal"]').forEach(btn => {
        btn.addEventListener('click', () => DOM.settingsModal.classList.add('hidden'));
    });
    document.getElementById('save-settings').addEventListener('click', saveSettings);

    function openSettings() {
        DOM.settingsDisplayName.value = State.user?.display_name || '';
        DOM.settingsUsername.value = State.user?.username || '';

        if (State.user?.avatar_path) {
            DOM.settingsAvatarImg.src = fileUrl(State.user.avatar_path);
            DOM.settingsAvatarImg.classList.remove('hidden');
            DOM.settingsAvatarPreview.classList.add('hidden');
        } else {
            DOM.settingsAvatarImg.classList.add('hidden');
            DOM.settingsAvatarPreview.classList.remove('hidden');
        }

        DOM.themeOptions.forEach(opt => {
            opt.classList.toggle('active', opt.dataset.theme === State.theme);
        });
        DOM.customThemeSettings.classList.toggle('hidden', State.theme !== 'custom');
        if (State.theme === 'custom') {
            DOM.customSelfColor.value = State.customColors.self;
            DOM.customOtherColor.value = State.customColors.other;
            DOM.customTextColor.value = State.customColors.text;
            DOM.selfColorHex.textContent = State.customColors.self;
            DOM.otherColorHex.textContent = State.customColors.other;
            DOM.textColorHex.textContent = State.customColors.text;
        }

        // Load prefs into toggles
        applyPrefs();

        DOM.settingsModal.classList.remove('hidden');
    }

    async function saveSettings() {
        const newName = DOM.settingsDisplayName.value.trim();
        if (newName && newName !== State.user?.display_name) {
            try {
                const res = await fetch(`${API}/api/profile?display_name=${encodeURIComponent(newName)}`, {
                    method: 'PATCH',
                    headers: { 'X-Auth-Token': State.token }
                });
                if (res.ok) {
                    State.user.display_name = newName;
                    updateHeader();
                    showToast('Display name updated', 'success');
                }
            } catch (e) {
                showToast('Failed to update name', 'error');
            }
        }

        const avatarFile = DOM.avatarInput.files[0];
        if (avatarFile) {
            const formData = new FormData();
            formData.append('file', avatarFile);
            try {
                const res = await fetch(`${API}/api/profile/avatar`, {
                    method: 'POST',
                    body: formData,
                    headers: { 'X-Auth-Token': State.token }
                });
                if (res.ok) {
                    const data = await res.json();
                    State.user.avatar_path = data.avatar_path;
                    DOM.settingsAvatarImg.src = fileUrl(data.avatar_path);
                    DOM.settingsAvatarImg.classList.remove('hidden');
                    DOM.settingsAvatarPreview.classList.add('hidden');
                    updateHeader();
                    showToast('Avatar updated', 'success');
                }
            } catch (e) {
                showToast('Failed to update avatar', 'error');
            }
        }

        // Save prefs from toggles
        if (DOM.toggleAutoScroll) State.prefs.autoScroll = DOM.toggleAutoScroll.checked;
        if (DOM.toggleScrollBtn) State.prefs.showScrollBtn = DOM.toggleScrollBtn.checked;
        if (DOM.toggleLatestBar) State.prefs.showLatestBar = DOM.toggleLatestBar.checked;
        savePrefs();

        const selectedTheme = document.querySelector('.theme-option.active')?.dataset.theme || 'light';
        if (selectedTheme === 'custom') {
            State.customColors.self = DOM.customSelfColor.value;
            State.customColors.other = DOM.customOtherColor.value;
            State.customColors.text = DOM.customTextColor.value;
            localStorage.setItem('custom-self', State.customColors.self);
            localStorage.setItem('custom-other', State.customColors.other);
            localStorage.setItem('custom-text', State.customColors.text);
        }
        applyTheme(selectedTheme);

        DOM.settingsModal.classList.add('hidden');
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

    // =============================================
    // Utility
    // =============================================
    function generateId() {
        return Date.now().toString(36) + Math.random().toString(36).substr(2, 9);
    }

    function truncate(str, n) {
        if (!str) return '';
        return str.length > n ? str.substr(0, n - 1) + '…' : str;
    }

    function escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function formatTime(ms) {
        if (!ms) return '';
        const date = new Date(ms);
        const now = new Date();
        const diff = now - date;
        if (diff < 60000) return 'just now';
        if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
        if (date.toDateString() === now.toDateString()) {
            return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        const yesterday = new Date(now);
        yesterday.setDate(yesterday.getDate() - 1);
        if (date.toDateString() === yesterday.toDateString()) {
            return 'Yesterday ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        return date.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
               date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function formatFileSize(bytes) {
        if (!bytes) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }

    function autoResize() {
        DOM.messageInput.style.height = 'auto';
        DOM.messageInput.style.height = Math.min(DOM.messageInput.scrollHeight, 120) + 'px';
    }

    // =============================================
    // Init
    // =============================================
    async function init() {
        applyPrefs();
        if (State.token) {
            try {
                const res = await fetch(`${API}/api/auth/verify`, {
                    headers: { 'X-Auth-Token': State.token }
                });
                if (res.ok) {
                    const data = await res.json();
                    State.user = data.user;
                    showChat();
                    connectWebSocket();
                    startTimestampUpdater();
                    return;
                }
            } catch (e) {}
            localStorage.removeItem('token');
            State.token = null;
        }
        showAuth();
    }

    function showAuth() {
        DOM.authScreen.classList.add('active');
        DOM.chatScreen.classList.remove('active');
    }

    function showChat() {
        DOM.authScreen.classList.remove('active');
        DOM.chatScreen.classList.add('active');
        updateHeader();
        startTimestampUpdater();
    }

    function updateHeader() {
        if (!State.user) return;
        const displayEl = document.getElementById('header-display-name');
        if (displayEl) displayEl.textContent = State.user.display_name || State.user.username;
        if (State.user.avatar_path) {
            const img = document.getElementById('header-avatar');
            const ph = document.getElementById('header-avatar-placeholder');
            if (img) { img.src = fileUrl(State.user.avatar_path); img.classList.remove('hidden'); }
            if (ph) ph.classList.add('hidden');
        }
    }

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

    document.getElementById('logout-btn').addEventListener('click', async () => {
        if (State.token) {
            try {
                await fetch(`${API}/api/auth/logout`, { method: 'POST', headers: { 'X-Auth-Token': State.token } });
            } catch (e) {}
        }
        if (State.ws) { State.ws.close(); State.ws = null; }
        localStorage.removeItem('token');
        State.token = null;
        State.user = null;
        State.messages.clear();
        State.messageOrder = [];
        State.onlineUsers.clear();
        State.typingUsers.clear();
        State.cursor = null;
        State.hasMore = true;
        State.latestUnseen = null;
        DOM.messagesList.innerHTML = '';
        DOM.emptyState.style.display = 'flex';
        hideLatestBar();
        showAuth();
        updateOnlineBadge();
    });

    if (window.Notification && Notification.permission === 'default') {
        Notification.requestPermission();
    }

    window.InfinityChat = { replyTo, editMessage, deleteMessage };

    init();
})();