/**
 * Google Workspace ADK - Dashboard Application
 * Alpine.js reactive component for chat, trace, and monitoring.
 * Supports image/file uploads and inline image rendering.
 */

// --- API Token helpers ---
// Token is read from localStorage and sent as Authorization: Bearer <token>.
// If API_TOKEN is not set on the server, all requests pass through without auth.

function getApiToken() {
    return localStorage.getItem('api_token') || '';
}

function setApiToken(token) {
    if (token) {
        localStorage.setItem('api_token', token);
    } else {
        localStorage.removeItem('api_token');
    }
}

/**
 * Wrapper around fetch() that injects the Authorization header when a token is stored.
 * Does NOT force Content-Type — callers set it explicitly where needed.
 * FormData uploads remain unaffected (browser sets multipart/form-data automatically).
 */
function apiFetch(url, options = {}) {
    const token = getApiToken();
    const headers = { ...options.headers };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    return fetch(url, { ...options, headers });
}

function dashboard() {
    return {
        // Chat state
        inputMessage: '',
        messages: [],
        streaming: false,
        streamBuffer: '',

        // File upload state
        pendingFiles: [],   // [{file, previewUrl, uploading, uploaded: {file_id, url, ...}}]
        dragOver: false,

        // Image modal
        imageModal: null,

        // Session state
        sessions: [],
        currentSessionId: null,
        userId: 'web-user',

        // System state
        agents: [],
        systemStatus: {},
        costs: {},
        traceEvents: [],
        showCostTab: false,

        // Voice state
        voiceEnabled: false,
        voiceListening: false,
        voiceSupported: false,
        ttsSupported: false,
        _recognition: null,
        _voiceAutoSend: false,

        // Gemini Live Voice state
        liveConnected: false,
        liveTranscript: '',
        _liveWs: null,
        _liveAudioCtx: null,
        _liveProcessor: null,
        _liveStream: null,

        // Polling
        _statusInterval: null,
        _hitlInterval: null,

        // HITL (Human-in-the-Loop) state
        pendingApprovals: [],
        hitlModal: null,       // confirmation object being reviewed
        hitlRejectReason: '',

        // Token auth state
        showTokenPrompt: false,

        handleUnauthorized() {
            // Called when any API request returns 401
            this.showTokenPrompt = true;
        },

        saveToken() {
            const input = document.getElementById('tokenInput');
            if (input && input.value.trim()) {
                setApiToken(input.value.trim());
                this.showTokenPrompt = false;
                // Reload data with new token
                this.init();
            }
        },

        clearToken() {
            setApiToken('');
            this.showTokenPrompt = false;
        },

        async init() {
            // Load initial data in parallel
            await Promise.all([
                this.loadAgents(),
                this.loadSessions(),
                this.loadStatus()
            ]);

            // Auto-create first session if none exist
            if (this.sessions.length === 0) {
                await this.newSession();
            }

            // Poll status every 15 seconds
            this._statusInterval = setInterval(() => this.loadStatus(), 15000);

            // Poll for pending HITL approvals every 5 seconds
            this._hitlInterval = setInterval(() => this.loadPendingHITL(), 5000);

            // Initialize voice
            this.initVoice();

            // Focus input
            this.$nextTick(() => {
                if (this.$refs.chatInput) this.$refs.chatInput.focus();
            });
        },

        // === Data Loading ===

        async loadAgents() {
            try {
                const res = await apiFetch('/api/agents');
                if (res.status === 401) { this.handleUnauthorized(); return; }
                if (res.ok) this.agents = await res.json();
            } catch (e) {
                console.error('Failed to load agents:', e);
            }
        },

        async loadSessions() {
            try {
                const res = await apiFetch('/api/sessions');
                if (res.status === 401) { this.handleUnauthorized(); return; }
                if (res.ok) this.sessions = await res.json();
            } catch (e) {
                console.error('Failed to load sessions:', e);
            }
        },

        async loadStatus() {
            try {
                const res = await apiFetch('/api/status');
                if (res.status === 401) { this.handleUnauthorized(); return; }
                if (res.ok) this.systemStatus = await res.json();
            } catch (e) {
                console.error('Failed to load status:', e);
            }
            try {
                const res = await apiFetch('/api/costs');
                if (res.ok) this.costs = await res.json();
            } catch (e) { /* non-critical */ }
        },

        async loadHistory(sessionId) {
            try {
                const [histRes, traceRes] = await Promise.all([
                    apiFetch(`/api/sessions/${sessionId}/history`),
                    apiFetch(`/api/trace/${sessionId}`)
                ]);
                if (histRes.ok) this.messages = await histRes.json();
                if (traceRes.ok) this.traceEvents = await traceRes.json();
                this.scrollToBottom();
            } catch (e) {
                console.error('Failed to load history:', e);
            }
        },

        // === Session Management ===

        async switchSession(session) {
            this.currentSessionId = session.session_id;
            await apiFetch(`/api/sessions/${session.session_id}/switch?user_id=${this.userId}`, {
                method: 'POST'
            });
            await this.loadHistory(session.session_id);
            await this.loadSessions();
        },

        async newSession() {
            try {
                const res = await apiFetch(`/api/sessions/new?user_id=${this.userId}`, {
                    method: 'POST'
                });
                if (res.ok) {
                    const data = await res.json();
                    this.currentSessionId = data.session_id;
                    this.messages = [];
                    this.traceEvents = [];
                    await this.loadSessions();
                }
            } catch (e) {
                console.error('Failed to create session:', e);
            }
        },

        // === File Upload ===

        handleFileSelect(event) {
            const files = event.target.files;
            if (files) this.addFiles(files);
            event.target.value = '';  // reset so same file can be re-selected
        },

        handleDrop(event) {
            this.dragOver = false;
            const files = event.dataTransfer.files;
            if (files) this.addFiles(files);
        },

        addFiles(fileList) {
            for (const file of fileList) {
                // Validate type
                const allowed = ['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp', 'application/pdf'];
                if (!allowed.includes(file.type)) {
                    console.warn(`Skipping unsupported file type: ${file.type}`);
                    continue;
                }

                // Create preview URL for images
                let previewUrl = null;
                if (file.type.startsWith('image/')) {
                    previewUrl = URL.createObjectURL(file);
                }

                this.pendingFiles.push({
                    file: file,
                    previewUrl: previewUrl,
                    uploading: false,
                    uploaded: null,
                });
            }
        },

        removePendingFile(idx) {
            const pf = this.pendingFiles[idx];
            if (pf.previewUrl) URL.revokeObjectURL(pf.previewUrl);
            this.pendingFiles.splice(idx, 1);
        },

        async uploadPendingFiles() {
            /**
             * Upload all pending files to /api/upload.
             * Returns array of upload results.
             */
            const results = [];
            for (const pf of this.pendingFiles) {
                if (pf.uploaded) {
                    results.push(pf.uploaded);
                    continue;
                }

                pf.uploading = true;
                try {
                    const formData = new FormData();
                    formData.append('file', pf.file);
                    formData.append('user_id', this.userId);
                    formData.append('session_id', this.currentSessionId || '');

                    // apiFetch: adds Authorization header but NOT Content-Type
                    // (browser sets multipart/form-data with boundary automatically for FormData)
                    const res = await apiFetch('/api/upload', {
                        method: 'POST',
                        body: formData
                    });

                    if (!res.ok) {
                        const err = await res.json();
                        throw new Error(err.detail || 'Upload failed');
                    }

                    const data = await res.json();
                    pf.uploaded = data;
                    results.push(data);
                } catch (e) {
                    console.error('Upload error:', e);
                    results.push({ error: e.message });
                } finally {
                    pf.uploading = false;
                }
            }
            return results;
        },

        // === Chat ===

        async sendMessage() {
            const message = this.inputMessage.trim();
            const hasFiles = this.pendingFiles.length > 0;
            if (!message && !hasFiles) return;
            if (this.streaming) return;

            this.inputMessage = '';
            this.streaming = true;
            this.streamBuffer = '';

            // Upload files first if any
            let uploadedFiles = [];
            let attachments = [];
            if (hasFiles) {
                uploadedFiles = await this.uploadPendingFiles();
                attachments = uploadedFiles
                    .filter(f => !f.error)
                    .map(f => ({
                        file_id: f.file_id,
                        filename: f.filename,
                        mime_type: f.mime_type,
                        url: f.url,
                    }));
            }

            // Build the message to send to the agent
            // If there are uploaded files, prepend file references
            let agentMessage = message;
            if (uploadedFiles.length > 0) {
                const fileRefs = uploadedFiles
                    .filter(f => !f.error)
                    .map(f => `[ATTACHED_FILE: file_id=${f.file_id}, name=${f.filename}, type=${f.mime_type}, base64_length=${f.base64?.length || 0}]`)
                    .join('\n');
                agentMessage = fileRefs + (message ? '\n\n' + message : '\n\nAnalyse the attached file(s).');
            }

            // Add user message to UI immediately
            this.messages.push({
                role: 'user',
                content: message || '(attached file)',
                attachments: attachments,
                timestamp: Date.now() / 1000
            });
            this.scrollToBottom();

            // Clean up pending files
            for (const pf of this.pendingFiles) {
                if (pf.previewUrl) URL.revokeObjectURL(pf.previewUrl);
            }
            this.pendingFiles = [];

            try {
                // Build request body - include file data for multimodal processing
                const requestBody = {
                    message: agentMessage,
                    user_id: this.userId,
                    session_id: this.currentSessionId,
                };

                // If we have uploaded files, include their base64 for the agent
                if (uploadedFiles.length > 0) {
                    requestBody.attachments = uploadedFiles
                        .filter(f => !f.error)
                        .map(f => ({
                            file_id: f.file_id,
                            mime_type: f.mime_type,
                            base64: f.base64,
                        }));
                }

                const response = await apiFetch('/api/chat/stream', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(requestBody)
                });

                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                }

                // Parse SSE stream
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                let currentEventType = 'text';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop() || '';

                    for (const line of lines) {
                        if (line.startsWith('event:')) {
                            currentEventType = line.substring(6).trim();
                        } else if (line.startsWith('data:')) {
                            const rawData = line.substring(5).trim();
                            if (rawData) {
                                this.handleSSEEvent(currentEventType, rawData);
                            }
                        }
                    }
                }

            } catch (err) {
                console.error('Stream error:', err);
                this.streamBuffer += '\n[Error: ' + err.message + ']';
            }

            // Finalize: move stream buffer into messages
            if (this.streamBuffer) {
                // TTS: read response aloud if enabled
                this.speakText(this.streamBuffer);

                this.messages.push({
                    role: 'assistant',
                    content: this.streamBuffer,
                    timestamp: Date.now() / 1000
                });
            }

            this.streaming = false;
            this.streamBuffer = '';
            this.scrollToBottom();

            // Refresh sessions list
            await this.loadSessions();

            // Refocus input
            this.$nextTick(() => {
                if (this.$refs.chatInput) this.$refs.chatInput.focus();
            });
        },

        handleSSEEvent(eventType, rawData) {
            switch (eventType) {
                case 'text':
                    this.streamBuffer += rawData;
                    this.scrollToBottom();
                    break;

                case 'image':
                    // Agent sent an inline image or video - render it
                    try {
                        const imgData = JSON.parse(rawData);
                        const tag = imgData.type === 'video' ? 'VIDEO' : 'IMAGE';
                        const mediaTag = `[${tag}:${imgData.url}:${imgData.alt || 'Generated media'}]`;
                        // Avoid duplicate tags (agent text may already contain the tag)
                        if (!this.streamBuffer.includes(mediaTag)) {
                            this.streamBuffer += `\n${mediaTag}\n`;
                        }
                        this.scrollToBottom();
                    } catch (e) {
                        console.warn('Failed to parse image event:', e);
                    }
                    break;

                case 'tool_call':
                case 'tool_response':
                    try {
                        const traceEntry = JSON.parse(rawData);
                        this.traceEvents.push(traceEntry);
                    } catch (e) { /* ignore parse errors */ }
                    break;

                case 'done':
                    try {
                        const doneData = JSON.parse(rawData);
                        if (doneData.session_id && !this.currentSessionId) {
                            this.currentSessionId = doneData.session_id;
                        }
                    } catch (e) { /* ignore */ }
                    break;

                case 'error':
                    this.streamBuffer += '\n[Error: ' + rawData + ']';
                    break;
            }
        },

        // === Image Modal ===

        openImageModal(url) {
            this.imageModal = url;
        },

        // === Formatting ===

        formatMessage(text) {
            if (!text) return '';

            // Debug: log if text contains media tags
            if (text.includes('[VIDEO:') || text.includes('[IMAGE:')) {
                console.log('formatMessage input contains media tag:', text.substring(text.indexOf('['), text.indexOf(']', text.indexOf('[')) + 1));
            }

            // Escape HTML.
            //
            // Quotes matter as much as angle brackets here. Everything below
            // interpolates captured text into src="..." and href="...", so a
            // URL containing a quote used to terminate the attribute and let
            // the rest of it become markup — an event handler, for instance.
            // The content being escaped arrives from email bodies, scraped
            // pages and documents, and it is stored in the chat history, so
            // that was a stored XSS with the API token sitting in localStorage.
            let safe = text
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');

            // === MEDIA TAGS - process BEFORE any other markdown ===

            // Inline videos [VIDEO:url:alt] - MUST be before IMAGE to avoid conflicts
            safe = safe.replace(/\[VIDEO:(\/api\/media\/[^\]:]+|https?:\/\/[^\]:]+):([^\]]*)\]/g,
                '<div class="chat-video"><video src="$1" controls preload="metadata"></video><div class="chat-image-caption">$2</div></div>');

            // GCS video references
            safe = safe.replace(/\[VIDEO:(gs:\/\/[^\]:]+):([^\]]*)\]/g,
                '<div class="chat-video"><em>Video stored at: $1</em><div class="chat-image-caption">$2</div></div>');

            // Inline images [IMAGE:url:alt]
            safe = safe.replace(/\[IMAGE:(\/api\/media\/[^\]:]+|https?:\/\/[^\]:]+):([^\]]*)\]/g,
                '<div class="chat-image"><img src="$1" alt="$2" loading="lazy" class="chat-image-zoom" data-full-src="$1"><div class="chat-image-caption">$2</div></div>');

            // GCS image references (fallback - show placeholder)
            safe = safe.replace(/\[IMAGE:(gs:\/\/[^\]:]+):([^\]]*)\]/g,
                '<div class="chat-image"><em>Image stored at: $1</em><div class="chat-image-caption">$2</div></div>');

            // Fallback: markdown links to /api/media/ rendered as images
            safe = safe.replace(/\[([^\]]*)\]\((\/api\/media\/[^)]+)\)/g,
                '<div class="chat-image"><img src="$2" alt="$1" loading="lazy" class="chat-image-zoom" data-full-src="$2"><div class="chat-image-caption">$1</div></div>');

            // Fallback: bare /api/media/ URLs on their own line
            safe = safe.replace(/(?:^|\n)(\/api\/media\/\S+)(?:\n|$)/gm,
                '\n<div class="chat-image"><img src="$1" alt="Generated image" loading="lazy" class="chat-image-zoom" data-full-src="$1"><div class="chat-image-caption">Generated image</div></div>\n');

            // Code blocks ```...```
            safe = safe.replace(/```(\w*)\n?([\s\S]*?)```/g,
                '<pre><code>$2</code></pre>');

            // Inline code `...`
            safe = safe.replace(/`([^`]+)`/g, '<code>$1</code>');

            // GCS storage links - replace with "image not publicly accessible" notice
            safe = safe.replace(/\[([^\]]*)\]\((https?:\/\/storage\.googleapis\.com\/[^)]+)\)/g,
                '<em class="text-muted">(Image generated but GCS link not publicly accessible)</em>');

            // Links [text](url)
            safe = safe.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,
                '<a href="$2" target="_blank" rel="noopener">$1</a>');

            // Bold **...**
            safe = safe.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');

            // Italic *...*
            safe = safe.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');

            // Status badges [Završeno] / [OK] / [Greška]
            safe = safe.replace(/\[(Zavr[sš]eno|OK|Uspje[sš]no)\]/gi,
                '<span class="badge badge-success">$1</span>');
            safe = safe.replace(/\[(Gre[sš]ka|Error|FAILED)\]/gi,
                '<span class="badge badge-error">$1</span>');
            safe = safe.replace(/\[(Info|Napomena)\]/gi,
                '<span class="badge badge-info">$1</span>');

            // Process line by line for lists and paragraphs
            const lines = safe.split('\n');
            let html = '';
            let inList = false;
            let listType = null;

            for (let i = 0; i < lines.length; i++) {
                const line = lines[i].trim();

                // Numbered list: 1. item or 1) item
                const olMatch = line.match(/^(\d+)[.)]\s+(.+)/);
                // Bullet list: - item or * item
                const ulMatch = line.match(/^[-*]\s+(.+)/);

                if (olMatch) {
                    if (!inList || listType !== 'ol') {
                        if (inList) html += listType === 'ol' ? '</ol>' : '</ul>';
                        html += '<ol>';
                        inList = true;
                        listType = 'ol';
                    }
                    html += '<li>' + olMatch[2] + '</li>';
                } else if (ulMatch) {
                    if (!inList || listType !== 'ul') {
                        if (inList) html += listType === 'ol' ? '</ol>' : '</ul>';
                        html += '<ul>';
                        inList = true;
                        listType = 'ul';
                    }
                    html += '<li>' + ulMatch[1] + '</li>';
                } else {
                    // Close any open list
                    if (inList) {
                        html += listType === 'ol' ? '</ol>' : '</ul>';
                        inList = false;
                        listType = null;
                    }

                    // Headings ### ...
                    if (line.match(/^#{1,3}\s+/)) {
                        const heading = line.replace(/^#{1,3}\s+/, '');
                        html += '<div class="msg-heading">' + heading + '</div>';
                    } else if (line === '') {
                        html += '<div class="msg-spacer"></div>';
                    } else if (line.startsWith('<div class="chat-image">')) {
                        // Don't wrap images in msg-line
                        html += line;
                    } else {
                        html += '<div class="msg-line">' + line + '</div>';
                    }
                }
            }
            if (inList) {
                html += listType === 'ol' ? '</ol>' : '</ul>';
            }

            return html;
        },

        formatSessionName(session) {
            const idx = this.sessions.indexOf(session);
            const num = this.sessions.length - idx;
            return 'Session ' + num;
        },

        getUtilization(info) {
            if (!info) return 0;
            if (info.project_bucket && typeof info.project_bucket.utilization === 'number') {
                return info.project_bucket.utilization;
            }
            return 0;
        },

        // === Voice ===

        initVoice() {
            // Check TTS support
            this.ttsSupported = 'speechSynthesis' in window;

            // Check STT support
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            if (!SpeechRecognition) {
                this.voiceSupported = false;
                return;
            }

            this.voiceSupported = true;
            const recognition = new SpeechRecognition();
            recognition.lang = 'hr-HR';
            recognition.continuous = true;
            recognition.interimResults = true;
            recognition.maxAlternatives = 1;

            // Accumulates all finalized segments across the entire session
            let _finalAccumulated = '';

            recognition.onresult = (event) => {
                // Rebuild from all results (not just delta) to avoid losing earlier segments
                let fullFinal = '';
                let interimTranscript = '';

                for (let i = 0; i < event.results.length; i++) {
                    if (event.results[i].isFinal) {
                        fullFinal += event.results[i][0].transcript + ' ';
                    } else {
                        interimTranscript += event.results[i][0].transcript;
                    }
                }

                // Show full accumulated text + current interim
                this.inputMessage = (fullFinal + interimTranscript).trim();
            };

            recognition.onend = () => {
                this.voiceListening = false;
            };

            recognition.onerror = (event) => {
                console.error('Speech recognition error:', event.error);
                this.voiceListening = false;
                this._voiceAutoSend = false;

                if (event.error === 'not-allowed') {
                    alert('Mikrofon nije dozvoljen. Omogucite pristup mikrofonu u postavkama preglednika.');
                }
            };

            this._recognition = recognition;
        },

        toggleListening() {
            if (!this._recognition) return;

            if (this.voiceListening) {
                this._recognition.stop();
                this.voiceListening = false;
                if (this.inputMessage.trim()) {
                    this.sendMessage();
                }
            } else {
                this.inputMessage = '';
                try {
                    this._recognition.start();
                    this.voiceListening = true;
                } catch (e) {
                    console.error('Failed to start recognition:', e);
                }
            }
        },

        async speakText(text) {
            if (!this.voiceEnabled) return;

            // Stop any current speech
            this.stopSpeaking();

            // Clean text
            let clean = text;
            clean = clean.replace(/\[Error:.*?\]/g, '');
            clean = clean.replace(/\[IMAGE:[^\]]*\]/g, '');
            clean = clean.replace(/\[VIDEO:[^\]]*\]/g, '');
            clean = clean.replace(/\[ATTACHED_FILE:[^\]]*\]/g, '');
            clean = clean.replace(/Error:\s*\d+\s+RESOURCE_EXHAUSTED[^\n]*/g, '');
            clean = clean.replace(/\{[^}]*'error'[^}]*\}/g, '');
            clean = clean.replace(/```[\s\S]*?```/g, '');
            clean = clean.replace(/`[^`]+`/g, '');
            clean = clean.replace(/\*\*(.*?)\*\*/g, '$1');
            clean = clean.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '$1');
            clean = clean.replace(/#{1,3}\s+/g, '');
            clean = clean.replace(/https?:\/\/\S+/g, '');
            clean = clean.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
            clean = clean.replace(/\n{2,}/g, '. ');
            clean = clean.replace(/\n/g, ' ');
            clean = clean.trim();
            if (!clean) return;

            // Try Gemini TTS first (natural voice)
            try {
                const resp = await fetch('/api/tts', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text: clean })
                });
                if (resp.ok) {
                    const pcmData = await resp.arrayBuffer();
                    const ctx = new AudioContext({ sampleRate: 24000 });
                    this._ttsCtx = ctx;
                    const int16 = new Int16Array(pcmData);
                    const float32 = new Float32Array(int16.length);
                    for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
                    const buf = ctx.createBuffer(1, float32.length, 24000);
                    buf.copyToChannel(float32, 0);
                    const src = ctx.createBufferSource();
                    src.buffer = buf;
                    src.connect(ctx.destination);
                    src.start();
                    return;
                }
            } catch (e) {
                console.warn('Gemini TTS failed, falling back to browser TTS:', e);
            }

            // Fallback: browser speechSynthesis
            if (!this.ttsSupported) return;
            const voices = speechSynthesis.getVoices();
            const hrVoice = voices.find(v => v.lang.startsWith('hr'));
            const chunks = clean.match(/.{1,180}(?:\s|$)/g) || [clean];
            const speakChunk = (i) => {
                if (i >= chunks.length) return;
                const utt = new SpeechSynthesisUtterance(chunks[i]);
                utt.lang = 'hr-HR';
                if (hrVoice) utt.voice = hrVoice;
                utt.onend = () => speakChunk(i + 1);
                utt.onerror = () => speakChunk(i + 1);
                speechSynthesis.speak(utt);
            };
            speakChunk(0);
        },

        stopSpeaking() {
            if (this._ttsCtx) {
                try { this._ttsCtx.close(); } catch(e) {}
                this._ttsCtx = null;
            }
            if (this.ttsSupported) speechSynthesis.cancel();
        },

        async toggleLiveVoice() {
            // If already connected — disconnect and clean up
            if (this._liveWs) {
                this._liveWs.close();
                this._liveWs = null;
                if (this._liveProcessor) { this._liveProcessor.disconnect(); this._liveProcessor = null; }
                if (this._liveAudioCtx) { this._liveAudioCtx.close(); this._liveAudioCtx = null; }
                if (this._liveStream) { this._liveStream.getTracks().forEach(t => t.stop()); this._liveStream = null; }
                this.liveConnected = false;
                this.liveTranscript = '';
                return;
            }

            // Connect to backend WebSocket
            const token = localStorage.getItem('api_token') || '';
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${proto}//${location.host}/api/live${token ? '?token=' + encodeURIComponent(token) : ''}`;
            const ws = new WebSocket(wsUrl);
            ws.binaryType = 'arraybuffer';
            this._liveWs = ws;

            // Playback: accumulate PCM16 into a rolling buffer, flush every ~200ms
            const playbackCtx = new AudioContext({ sampleRate: 24000 });
            let nextStartTime = 0;
            let pendingSamples = [];

            const flushAudio = () => {
                if (pendingSamples.length === 0) return;
                const combined = new Float32Array(pendingSamples.length);
                combined.set(pendingSamples);
                pendingSamples = [];
                const buf = playbackCtx.createBuffer(1, combined.length, 24000);
                buf.copyToChannel(combined, 0);
                const src = playbackCtx.createBufferSource();
                src.buffer = buf;
                src.connect(playbackCtx.destination);
                const startAt = Math.max(playbackCtx.currentTime + 0.05, nextStartTime);
                src.start(startAt);
                nextStartTime = startAt + buf.duration;
            };

            // Flush accumulated audio every 150ms
            const flushInterval = setInterval(flushAudio, 150);

            const scheduleChunk = (pcmArrayBuffer) => {
                const int16 = new Int16Array(pcmArrayBuffer);
                for (let i = 0; i < int16.length; i++) {
                    pendingSamples.push(int16[i] / 32768);
                }
                // If we have > 0.5s of audio buffered, flush immediately
                if (pendingSamples.length > 12000) flushAudio();
            };

            ws.onmessage = (evt) => {
                if (typeof evt.data === 'string') {
                    try {
                        const msg = JSON.parse(evt.data);
                        if (msg.type === 'transcript') this.liveTranscript = msg.text;
                        if (msg.type === 'error') console.error('Live error:', msg.message);
                    } catch (e) {}
                } else {
                    // PCM16 audio from Gemini — schedule immediately
                    scheduleChunk(evt.data);
                }
            };

            ws.onopen = async () => {
                this.liveConnected = true;
                try {
                    // Capture mic at 16kHz mono
                    this._liveStream = await navigator.mediaDevices.getUserMedia({
                        audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true }
                    });
                    this._liveAudioCtx = new AudioContext({ sampleRate: 16000 });
                    const source = this._liveAudioCtx.createMediaStreamSource(this._liveStream);
                    const processor = this._liveAudioCtx.createScriptProcessor(8192, 1, 1);
                    processor.onaudioprocess = (e) => {
                        if (ws.readyState !== WebSocket.OPEN) return;
                        const float32 = e.inputBuffer.getChannelData(0);
                        const int16 = new Int16Array(float32.length);
                        for (let i = 0; i < float32.length; i++) {
                            int16[i] = Math.max(-32768, Math.min(32767, float32[i] * 32768));
                        }
                        ws.send(int16.buffer);
                    };
                    source.connect(processor);
                    processor.connect(this._liveAudioCtx.destination);
                    this._liveProcessor = processor;
                } catch (err) {
                    console.error('Live mic error:', err);
                    alert('Ne mogu pristupiti mikrofonu: ' + err.message);
                    ws.close();
                }
            };

            ws.onclose = (evt) => {
                clearInterval(flushInterval);
                flushAudio(); // play remaining buffered audio
                this.liveConnected = false;
                this._liveWs = null;
                if (evt.code === 4001) alert('Live Voice: greška autentikacije.');
                if (evt.code === 4002) alert('Live Voice nije dostupan — GEMINI_API_KEY nije postavljen na serveru.');
            };

            ws.onerror = (e) => {
                console.error('Live WS error:', e);
                this.liveConnected = false;
                alert('Live Voice: ne mogu se spojiti na server. Provjeri konzolu (F12) za detalje.');
            };
        },

        scrollToBottom() {
            this.$nextTick(() => {
                const container = this.$refs.messagesContainer;
                if (container) {
                    container.scrollTop = container.scrollHeight;
                }
            });
        },

        // === HITL Approval ===

        async loadPendingHITL() {
            try {
                const res = await apiFetch('/api/hitl/pending');
                if (res.ok) this.pendingApprovals = await res.json();
            } catch (e) {
                // Silent — HITL polling is non-critical
            }
        },

        openHITLModal(confirmation) {
            this.hitlModal = confirmation;
            this.hitlRejectReason = '';
        },

        closeHITLModal() {
            this.hitlModal = null;
            this.hitlRejectReason = '';
        },

        async approveHITL(confirmationId) {
            try {
                const res = await apiFetch(`/api/hitl/${confirmationId}/approve`, { method: 'POST' });
                if (res.ok) {
                    this.pendingApprovals = this.pendingApprovals.filter(a => a.confirmation_id !== confirmationId);
                    this.closeHITLModal();
                }
            } catch (e) {
                console.error('HITL approve failed:', e);
            }
        },

        async rejectHITL(confirmationId) {
            try {
                const reason = this.hitlRejectReason || 'User rejected';
                const res = await apiFetch(
                    `/api/hitl/${confirmationId}/reject?reason=${encodeURIComponent(reason)}`,
                    { method: 'POST' }
                );
                if (res.ok) {
                    this.pendingApprovals = this.pendingApprovals.filter(a => a.confirmation_id !== confirmationId);
                    this.closeHITLModal();
                }
            } catch (e) {
                console.error('HITL reject failed:', e);
            }
        },
    };
}

// Image zoom, delegated.
//
// These handlers used to be written inline while assembling the message HTML,
// which meant a crafted URL could contribute its own JavaScript. The element
// now carries only data, and the behaviour lives here.
document.addEventListener('click', (event) => {
    const img = event.target.closest && event.target.closest('img.chat-image-zoom');
    if (!img) return;
    const root = document.querySelector('[x-data]');
    const scope = root && root._x_dataStack && root._x_dataStack[0];
    if (scope && typeof scope.openImageModal === 'function') {
        scope.openImageModal(img.dataset.fullSrc);
    }
});
