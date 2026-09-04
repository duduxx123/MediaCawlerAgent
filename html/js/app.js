(function () {
    'use strict';

    const PAGE_SIZE = 20;

    const $ = (id) => document.getElementById(id);

    // 各 Tab 的表格列定义
    const COLUMNS = {
        comments: [
            { key: 'keyword', label: '关键词', type: 'tag' },
            { key: 'platform_label', label: '平台', type: 'platform' },
            { key: 'video_url', label: '视频链接', type: 'link' },
            { key: 'video_title', label: '视频标题', type: 'title' },
            { key: 'commenter_name', label: '评论者昵称' },
            { key: 'commenter_public_id', label: '平台账号（抖音号/小红书号）', type: 'mono' },
            { key: 'commenter_internal_id', label: '内部用户ID', type: 'mono' },
            { key: 'comment', label: '评论内容', type: 'comment' },
            { key: 'like_count', label: '点赞', type: 'num' },
            { key: 'comment_time', label: '评论时间', type: 'time' },
            { key: 'fetch_time', label: '获取时间', type: 'time' },
        ],
        contents: [
            { key: 'keyword', label: '关键词', type: 'tag' },
            { key: 'platform_label', label: '平台', type: 'platform' },
            { key: 'cover_url', label: '封面', type: 'cover' },
            { key: 'url', label: '视频链接', type: 'link' },
            { key: 'title', label: '标题', type: 'title' },
            { key: 'nickname', label: '作者' },
            { key: 'creator_public_id', label: '作者平台账号', type: 'mono' },
            { key: 'creator_internal_id', label: '作者内部ID', type: 'mono' },
            { key: 'like_count', label: '点赞', type: 'num' },
            { key: 'comment_count', label: '评论数', type: 'num' },
            { key: 'create_time', label: '发布时间', type: 'time' },
            { key: 'fetch_time', label: '获取时间', type: 'time' },
        ],
    };

    const PLATFORM_COLORS = {
        '抖音': { bg: '#e6f0ff', color: '#2b85f6' },
        'B站': { bg: '#ffe9ec', color: '#fb7299' },
        '小红书': { bg: '#ffe9e9', color: '#ff2442' },
        '快手': { bg: '#fff3e6', color: '#e67e22' },
        '微博': { bg: '#fff8e6', color: '#d4922a' },
        '贴吧': { bg: '#e8f7f0', color: '#1f9d55' },
        '知乎': { bg: '#e6f4ff', color: '#0a7bd4' },
    };

    const TAG_COLORS = [
        { bg: '#e6f0ff', color: '#2b85f6' },
        { bg: '#e7f7e7', color: '#2f9e5f' },
        { bg: '#fff3e6', color: '#e67e22' },
        { bg: '#f3e6ff', color: '#8b5cf6' },
        { bg: '#ffe6f0', color: '#d63384' },
    ];

    const state = {
        tab: 'comments',
        comments: [],
        contents: [],
        wordclouds: [],
        selectedWordcloud: '',
        selectedComments: new Set(), // 批量删除选中：key = platform + '' + comment_id
        deleting: false,
        page: 1,
        // 智能体对话状态
        sessionId: '',
        agentAvailable: false,
        agentInitialized: false,
        agentBusy: false,
        agentGotToken: false,
        agentFinished: false,
        agentLastBubble: null,
        agentLastTextNode: null,
    };

    // ---------- 工具函数 ----------
    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // 时间戳可能是秒（如 create_time）或毫秒（如 last_modify_ts）
    function fmtTime(ts) {
        if (ts === null || ts === undefined || ts === '') return '-';
        const n = Number(ts);
        if (!n || Number.isNaN(n)) return '-';
        const ms = n < 1e12 ? n * 1000 : n;
        const d = new Date(ms);
        const p = (x) => String(x).padStart(2, '0');
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }

    function dateStr(ts) {
        if (ts === null || ts === undefined || ts === '') return null;
        const n = Number(ts);
        if (!n || Number.isNaN(n)) return null;
        const d = new Date(n < 1e12 ? n * 1000 : n);
        const p = (x) => String(x).padStart(2, '0');
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    }

    function fmtNum(v) {
        if (v === null || v === undefined || v === '') return '-';
        const n = Number(v);
        if (Number.isNaN(n)) return '-';
        return n.toLocaleString('zh-CN');
    }

    function tagColor(keyword) {
        let h = 0;
        for (const ch of String(keyword)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
        return TAG_COLORS[h % TAG_COLORS.length];
    }

    // ---------- 数据加载 ----------
    async function loadData() {
        $('loading').hidden = false;
        $('error').hidden = true;
        $('empty').hidden = true;
        try {
            const [commentsRes, contentsRes, wordcloudsRes] = await Promise.all([
                fetch('/api/leads/comments'),
                fetch('/api/leads/contents'),
                fetch('/api/leads/wordclouds'),
            ]);
            if (!commentsRes.ok || !contentsRes.ok || !wordcloudsRes.ok) throw new Error('HTTP ' + [commentsRes, contentsRes, wordcloudsRes].find((r) => !r.ok).status);

            const commentsJson = await commentsRes.json();
            const contentsJson = await contentsRes.json();
            const wordcloudsJson = await wordcloudsRes.json();

            state.comments = commentsJson.leads || [];
            state.contents = contentsJson.contents || [];
            state.wordclouds = wordcloudsJson.wordclouds || [];

            pruneSelection(); // 数据刷新后清理失效的勾选
            renderFilterOptions();
            render();
        } catch (err) {
            $('loading').hidden = true;
            $('error').hidden = false;
            $('error-text').textContent = '数据加载失败：' + err.message +
                '。请确认后端已启动（uv run uvicorn api.main:app --port 8080），并通过 http://localhost:8080/leads 访问本页。';
        } finally {
            $('loading').hidden = true;
        }
    }

    // 聚合平台与关键词选项
    function renderFilterOptions() {
        const all = state.comments.concat(state.contents);
        const platforms = [...new Set(all.map((r) => r.platform))];
        const keywords = [...new Set(all.map((r) => r.keyword).filter(Boolean))].sort();

        const platformSel = $('f-platform');
        const keepPlatform = platformSel.value;
        platformSel.innerHTML = '<option value="">全部平台</option>' + platforms.map((p) => {
            const label = PLATFORM_LABEL(p);
            return `<option value="${escapeHtml(p)}">${escapeHtml(label)}</option>`;
        }).join('');

        const keywordSel = $('f-keyword');
        const keepKeyword = keywordSel.value;
        keywordSel.innerHTML = '<option value="">全部关键词</option>' + keywords.map((k) =>
            `<option value="${escapeHtml(k)}">${escapeHtml(k)}</option>`
        ).join('');

        // 重新加载后尽量保留已选项
        if ([...platformSel.options].some((o) => o.value === keepPlatform)) platformSel.value = keepPlatform;
        if ([...keywordSel.options].some((o) => o.value === keepKeyword)) keywordSel.value = keepKeyword;
    }

    function PLATFORM_LABEL(p) {
        const map = {
            douyin: '抖音', dy: '抖音',
            bili: 'B站', bilibili: 'B站',
            xhs: '小红书', kuaishou: '快手', ks: '快手',
            weibo: '微博', wb: '微博', tieba: '贴吧', zhihu: '知乎',
        };
        return map[p] || p;
    }

    // ---------- 批量删除 ----------
    function commentKey(row) {
        // '' 控制字符不会出现在 platform/comment_id 中，天然无碰撞
        return (row.platform || '') + '' + (row.comment_id || '');
    }

    function checkCell(row) {
        if (!row.comment_id) return '<td class="td-check"></td>'; // 无评论 ID 的记录不可删
        const key = commentKey(row);
        const checked = state.selectedComments.has(key) ? ' checked' : '';
        return `<td class="td-check"><input type="checkbox" class="row-check" ` +
            `data-platform="${escapeHtml(row.platform)}" data-comment-id="${escapeHtml(row.comment_id)}"${checked}></td>`;
    }

    function syncCheckboxes(pageRows) {
        // 渲染（innerHTML 重建）后同步当前页复选框与全选框状态
        const pageKeys = pageRows.map(commentKey);
        const all = pageKeys.length > 0 && pageKeys.every((k) => state.selectedComments.has(k));
        const head = $('check-all');
        if (head) head.checked = all;
        updateBatchDeleteButton();
    }

    function updateBatchDeleteButton() {
        const btn = $('btn-batch-delete');
        if (!btn) return;
        const n = state.selectedComments.size;
        btn.textContent = n ? `批量删除（已选 ${n} 条）` : '批量删除（已选 0 条）';
        btn.disabled = n === 0 || state.deleting;
    }

    function pruneSelection() {
        // 数据刷新后清理已不存在的选中项，防幽灵勾选
        if (!state.selectedComments.size) return;
        const valid = new Set(state.comments.map(commentKey));
        for (const k of state.selectedComments) {
            if (!valid.has(k)) state.selectedComments.delete(k);
        }
    }

    async function batchDeleteComments() {
        if (state.deleting) return;
        const items = [...state.selectedComments].map((k) => {
            const sep = k.indexOf('');
            return { platform: k.slice(0, sep), comment_id: k.slice(sep + 1) };
        });
        if (!items.length) return;

        if (!window.confirm(
            `确定删除选中的 ${items.length} 条评论吗？\n其下的二级回复将一并删除，删除后不可恢复。`
        )) return;

        state.deleting = true;
        updateBatchDeleteButton();
        try {
            const res = await fetch('/api/leads/comments/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items: items }),
            });
            const data = await res.json().catch(() => ({}));
            if (res.ok) {
                state.selectedComments.clear();
                await loadData();
                let msg = `已删除 ${data.deleted} 条评论（含 ${data.cascaded} 条二级回复）。`;
                if (data.not_found && data.not_found.length) msg += `\n其中 ${data.not_found.length} 条已不存在。`;
                window.alert(msg);
            } else if (res.status === 409) {
                window.alert(data.detail || '爬取任务进行中，请等待其完成后再删除。');
            } else if (res.status === 404) {
                state.selectedComments.clear();
                await loadData();
                window.alert('所选评论已不存在，列表已刷新。');
            } else {
                window.alert('删除失败：' + (data.detail || 'HTTP ' + res.status));
            }
        } catch (err) {
            window.alert('删除请求失败：' + (err && err.message ? err.message : err));
        } finally {
            state.deleting = false;
            updateBatchDeleteButton();
        }
    }

    async function generateWordclouds() {
        // 对所有有评论数据的平台按需生成词云（从 SQLite 评论库生成）
        const platforms = [...new Set(state.comments.map((r) => r.platform).filter(Boolean))];
        if (!platforms.length) {
            window.alert('暂无评论数据，无法生成词云。请先抓取评论。');
            return;
        }
        const btn = $('btn-wordcloud-generate');
        btn.disabled = true;
        const results = [];
        for (const p of platforms) {
            try {
                const res = await fetch('/api/leads/wordclouds/' + encodeURIComponent(p) + '/generate', { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (res.ok) results.push(`${PLATFORM_LABEL(p)}：成功（${data.comments} 条评论）`);
                else results.push(`${PLATFORM_LABEL(p)}：${data.detail || 'HTTP ' + res.status}`);
            } catch (err) {
                results.push(`${PLATFORM_LABEL(p)}：${err && err.message ? err.message : '请求失败'}`);
            }
        }
        btn.disabled = false;
        window.alert('词云生成结果：\n' + results.join('\n'));
        await loadData();
    }

    // ---------- 筛选 ----------
    function currentData() {
        return state.tab === 'comments' ? state.comments : state.contents;
    }

    function timeField() {
        return state.tab === 'comments' ? 'comment_time' : 'create_time';
    }

    function filteredData() {
        const platform = $('f-platform').value;
        const keyword = $('f-keyword').value;
        const search = $('f-search').value.trim().toLowerCase();
        const ts = $('f-time-start').value;
        const te = $('f-time-end').value;
        const fs = $('f-fetch-start').value;
        const fe = $('f-fetch-end').value;
        const tf = timeField();

        return currentData().filter((row) => {
            if (platform && row.platform !== platform) return false;
            if (keyword && row.keyword !== keyword) return false;
            if (search) {
                const hay = state.tab === 'comments'
                    ? [row.comment, row.commenter_name, row.video_title, row.commenter_public_id,
                        row.commenter_internal_id, row.commenter_id].join(' ')
                    : [row.title, row.desc, row.nickname, row.creator_public_id,
                        row.creator_internal_id, row.creator_hash].join(' ');
                if (!String(hay).toLowerCase().includes(search)) return false;
            }
            const d = dateStr(row[tf]);
            if (ts && (d === null || d < ts)) return false;
            if (te && (d === null || d > te)) return false;
            const fd = dateStr(row.fetch_time);
            if (fs && (fd === null || fd < fs)) return false;
            if (fe && (fd === null || fd > fe)) return false;
            return true;
        });
    }

    // ---------- 渲染 ----------
    function renderCell(col, row) {
        const value = row[col.key];
        switch (col.type) {
            case 'tag': {
                if (!value) return '<span class="muted">-</span>';
                const c = tagColor(value);
                return `<span class="tag" style="background:${c.bg};color:${c.color}">${escapeHtml(value)}</span>`;
            }
            case 'platform': {
                const pc = PLATFORM_COLORS[value] || { bg: '#e5e7eb', color: '#4b5563' };
                return `<span class="badge" style="background:${pc.bg};color:${pc.color}">${escapeHtml(value || '-')}</span>`;
            }
            case 'link': {
                if (!value) return '<span class="muted">-</span>';
                return `<a class="link" href="${escapeHtml(value)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(value)}">${escapeHtml(value)}</a>`;
            }
            case 'cover': {
                if (!value) return '<span class="muted">-</span>';
                return `<img class="cover" loading="lazy" src="${escapeHtml(value)}" alt="" onerror="this.outerHTML='<span class=&quot;no-img&quot;>无图</span>'">`;
            }
            case 'title': {
                if (!value) return '<span class="muted">-</span>';
                return `<span class="cell-title" title="${escapeHtml(value)}">${escapeHtml(value)}</span>`;
            }
            case 'comment': {
                if (!value) return '<span class="muted">-</span>';
                return `<span class="cell-comment" title="${escapeHtml(value)}">${escapeHtml(value)}</span>`;
            }
            case 'time':
                return `<span class="time">${fmtTime(value)}</span>`;
            case 'num':
                return `<span class="num">${fmtNum(value)}</span>`;
            case 'mono': {
                if (!value) return '<span class="muted">-</span>';
                return `<span class="mono" title="${escapeHtml(value)}">${escapeHtml(value)}</span>`;
            }
            default: {
                if (value === null || value === undefined || value === '') return '<span class="muted">-</span>';
                return `<span title="${escapeHtml(value)}">${escapeHtml(value)}</span>`;
            }
        }
    }

    function render() {
        if (state.tab === 'wordcloud') {
            renderWordcloud();
            return;
        }
        if (state.tab === 'agent') return;
        const columns = COLUMNS[state.tab];
        const isComments = state.tab === 'comments';
        const filtered = filteredData();

        // 表头（评论页前置勾选列，不进 COLUMNS 的 type 体系）
        $('table-head').innerHTML = '<tr>' + (isComments
            ? '<th class="th-check"><input type="checkbox" id="check-all" title="全选本页"></th>'
            : '') + columns.map((c) => `<th>${escapeHtml(c.label)}</th>`).join('') + '</tr>';

        // 分页切片
        const total = filtered.length;
        const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
        if (state.page > totalPages) state.page = totalPages;
        const start = (state.page - 1) * PAGE_SIZE;
        const pageRows = filtered.slice(start, start + PAGE_SIZE);

        // 表体
        if (pageRows.length === 0) {
            $('table-body').innerHTML = '';
            $('empty').hidden = false;
            $('empty-text').textContent = total === 0 ? '暂无数据' : '当前页无数据';
        } else {
            $('empty').hidden = true;
            $('table-body').innerHTML = pageRows.map((row) =>
                '<tr>' + (isComments ? checkCell(row) : '') +
                columns.map((c) => `<td>${renderCell(c, row)}</td>`).join('') + '</tr>'
            ).join('');
            if (isComments) syncCheckboxes(pageRows);
        }

        // 统计
        const allTotal = currentData().length;
        $('stats').innerHTML = total === allTotal
            ? `共 <b>${total}</b> 条记录`
            : `筛选结果 <b>${total}</b> 条 / 全部 ${allTotal} 条`;

        // 批量删除按钮：仅评论页可见
        $('btn-batch-delete').hidden = !isComments;
        updateBatchDeleteButton();

        renderPagination(total, totalPages);
    }

    function renderWordcloud() {
        const select = $('wordcloud-select');
        const current = state.selectedWordcloud;
        select.innerHTML = state.wordclouds.length
            ? state.wordclouds.map((item, i) => `<option value="${i}">${escapeHtml(item.platform_label)} · ${escapeHtml(item.filename)}</option>`).join('')
            : '<option value="">暂无词云</option>';
        if (state.wordclouds.length) {
            const index = current && Number(current) < state.wordclouds.length ? Number(current) : 0;
            state.selectedWordcloud = String(index);
            select.value = state.selectedWordcloud;
        }
        const item = state.wordclouds[Number(state.selectedWordcloud)];
        if (!item) {
            $('wordcloud-content').innerHTML = '<div class="wordcloud-empty">暂无词云文件。点击上方「生成词云」从已采集评论生成。</div>';
            return;
        }
        const words = item.top_words || [];
        $('wordcloud-content').innerHTML = `
            <div class="wordcloud-card">
                <img class="wordcloud-image" src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.platform_label)}评论词云">
                <div class="wordcloud-words">
                    <h3>高频词</h3>
                    ${words.length ? words.map((word) => `<span class="word-item">${escapeHtml(word.word)} <b>${fmtNum(word.count)}</b></span>`).join('') : '<span class="muted">暂无词频数据</span>'}
                </div>
            </div>`;
    }

    function renderPagination(total, totalPages) {
        const wrap = $('pagination');
        if (total === 0) {
            wrap.innerHTML = '';
            return;
        }
        const page = state.page;
        const numbers = pageNumbers(page, totalPages);

        let html = `<span class="page-info">共 ${total} 条 · 第 ${page} / ${totalPages} 页</span>`;
        html += '<div class="page-btns">';
        html += `<button class="page-btn" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>‹ 上一页</button>`;
        for (const n of numbers) {
            if (n === '…') {
                html += '<span class="page-info">…</span>';
            } else {
                html += `<button class="page-btn ${n === page ? 'current' : ''}" data-page="${n}">${n}</button>`;
            }
        }
        html += `<button class="page-btn" data-page="${page + 1}" ${page >= totalPages ? 'disabled' : ''}>下一页 ›</button>`;
        html += '</div>';
        wrap.innerHTML = html;
    }

    function pageNumbers(current, total) {
        if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
        const set = new Set([1, total, current - 1, current, current + 1]);
        const nums = [...set].filter((n) => n >= 1 && n <= total).sort((a, b) => a - b);
        const out = [];
        let prev = 0;
        for (const n of nums) {
            if (n - prev > 1) out.push('…');
            out.push(n);
            prev = n;
        }
        return out;
    }

    // ---------- 智能体对话 ----------
    function getSessionId() {
        if (!state.sessionId) {
            let sid = '';
            try { sid = localStorage.getItem('mc_agent_session') || ''; } catch (e) { /* 隐私模式下忽略 */ }
            if (!sid) {
                sid = (crypto.randomUUID && crypto.randomUUID()) || ('sid-' + Date.now() + '-' + Math.floor(Math.random() * 1e6));
                try { localStorage.setItem('mc_agent_session', sid); } catch (e) { /* ignore */ }
            }
            state.sessionId = sid;
        }
        return state.sessionId;
    }

    async function checkAgentStatus() {
        const statusEl = $('agent-status');
        statusEl.classList.remove('ok', 'err');
        try {
            const res = await fetch('/api/agent/status');
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const status = await res.json();
            state.agentAvailable = !!status.available;
            if (status.available) {
                const tools = status.tools || [];
                statusEl.textContent = `已连接 · ${status.model || 'LLM'} · ${tools.length} 个工具`;
                statusEl.title = tools.join('、');
                statusEl.classList.add('ok');
            } else {
                statusEl.textContent = '模型服务不可用（未配置 Key）';
                statusEl.classList.add('err');
            }
        } catch (e) {
            state.agentAvailable = false;
            statusEl.textContent = '服务连接失败';
            statusEl.classList.add('err');
        }
        $('chat-send').disabled = !state.agentAvailable || state.agentBusy;
    }

    function initAgent() {
        getSessionId();
        checkAgentStatus();
    }

    function showChatError(text) {
        const el = $('chat-error');
        el.textContent = text;
        el.hidden = false;
    }

    function scrollChatToBottom(force) {
        const box = $('chat-messages');
        if (force || box.scrollTop + box.clientHeight >= box.scrollHeight - 80) {
            box.scrollTop = box.scrollHeight;
        }
    }

    function appendMsg(role, text) {
        const empty = $('chat-empty');
        if (empty) empty.hidden = true;
        const wrap = document.createElement('div');
        wrap.className = 'msg ' + (role === 'user' ? 'msg-user' : 'msg-assistant');
        const bubble = document.createElement('div');
        bubble.className = 'msg-bubble';
        bubble.textContent = text;
        wrap.appendChild(bubble);
        $('chat-messages').appendChild(wrap);
        scrollChatToBottom(true);
        return bubble;
    }

    function appendAssistantChunk(chunk) {
        if (!state.agentLastBubble) {
            state.agentLastBubble = appendMsg('assistant', '');
            state.agentLastTextNode = document.createTextNode('');
            state.agentLastBubble.appendChild(state.agentLastTextNode);
        }
        state.agentLastTextNode.appendData(chunk);
        scrollChatToBottom(false);
    }

    function setToolStatus(name, opts) {
        const el = document.createElement('div');
        el.className = 'tool-status' + (opts.ok === false ? ' err' : opts.ok === true ? ' ok' : '');
        el.textContent = opts.ok === false
            ? `工具 ${name} 执行失败`
            : opts.ok === true
                ? `工具 ${name} 已完成`
                : `调用工具 ${name}…`;
        $('chat-messages').appendChild(el);
        scrollChatToBottom(true);
    }

    function finishAgentTurn() {
        state.agentBusy = false;
        state.agentFinished = true;
        $('chat-send').disabled = !state.agentAvailable;
    }

    async function streamAgentChat(message) {
        const res = await fetch('/api/agent/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: message, history: [], session_id: getSessionId() }),
        });
        if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);

        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) >= 0) {
                const frame = buffer.slice(0, idx);
                buffer = buffer.slice(idx + 2);
                const line = frame.trim();
                if (!line.startsWith('data:')) continue;
                let evt;
                try { evt = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
                if (evt.type === 'token') {
                    state.agentGotToken = true;
                    appendAssistantChunk(evt.content || '');
                } else if (evt.type === 'tool_start') {
                    setToolStatus(evt.name, {});
                } else if (evt.type === 'tool_end') {
                    setToolStatus(evt.name, { ok: !!evt.result_ok });
                } else if (evt.type === 'done') {
                    if (!state.agentGotToken) appendAssistantChunk(evt.reply || '（无内容回复）');
                    finishAgentTurn();
                } else if (evt.type === 'error') {
                    showChatError(evt.message || '对话出错');
                    finishAgentTurn();
                }
            }
        }
        if (!state.agentFinished) {
            showChatError('连接中断，请重试');
            finishAgentTurn();
        }
    }

    async function sendAgentMessage() {
        const text = $('chat-input').value.trim();
        if (!text || state.agentBusy) return;
        appendMsg('user', text);
        $('chat-input').value = '';
        $('chat-error').hidden = true;
        state.agentBusy = true;
        state.agentGotToken = false;
        state.agentFinished = false;
        state.agentLastBubble = null;
        state.agentLastTextNode = null;
        $('chat-send').disabled = true;
        try {
            await streamAgentChat(text);
        } catch (err) {
            showChatError(String(err && err.message ? err.message : err));
            finishAgentTurn();
        }
    }

    // ---------- Tab / 事件 ----------
    function switchTab(tab) {
        if (state.tab === tab) return;
        state.tab = tab;
        state.page = 1;

        document.querySelectorAll('.tab').forEach((el) => {
            el.classList.toggle('active', el.dataset.tab === tab);
        });

        // 更新动态文案
        $('time-label').textContent = tab === 'comments' ? '评论时间' : '发布时间';
        $('f-search').placeholder = tab === 'comments'
            ? '查找评论内容 / 昵称 / 视频标题'
            : '查找标题 / 描述 / 作者';

        const isAgent = tab === 'agent';
        const isWordcloud = tab === 'wordcloud';
        const isData = !isWordcloud && !isAgent;
        $('filters').hidden = !isData;
        document.querySelector('.stats-row').hidden = !isData;
        document.querySelector('.table-wrap').hidden = !isData;
        $('pagination').hidden = !isData;
        $('wordcloud-panel').hidden = !isWordcloud;
        $('agent-panel').hidden = !isAgent;
        $('btn-batch-delete').hidden = tab !== 'comments';

        if (isAgent && !state.agentInitialized) {
            state.agentInitialized = true;
            initAgent();
        }

        render();
    }

    function resetFilters() {
        $('f-platform').value = '';
        $('f-keyword').value = '';
        $('f-search').value = '';
        $('f-time-start').value = '';
        $('f-time-end').value = '';
        $('f-fetch-start').value = '';
        $('f-fetch-end').value = '';
        state.page = 1;
        render();
    }

    function bindEvents() {
        document.querySelectorAll('.tab').forEach((el) => {
            el.addEventListener('click', () => switchTab(el.dataset.tab));
        });

        $('btn-search').addEventListener('click', () => { state.page = 1; render(); });
        $('btn-reset').addEventListener('click', resetFilters);
        $('btn-refresh').addEventListener('click', loadData);
        $('wordcloud-select').addEventListener('change', (e) => {
            state.selectedWordcloud = e.target.value;
            renderWordcloud();
        });
        $('btn-wordcloud-refresh').addEventListener('click', loadData);
        $('btn-wordcloud-generate').addEventListener('click', generateWordclouds);

        // 批量删除：勾选列 change 事件冒泡委托到表格
        $('data-table').addEventListener('change', (e) => {
            const t = e.target;
            if (t.id === 'check-all') {
                const checked = t.checked;
                document.querySelectorAll('.row-check').forEach((cb) => {
                    cb.checked = checked;
                    const key = (cb.dataset.platform || '') + '' + (cb.dataset.commentId || '');
                    if (checked) state.selectedComments.add(key);
                    else state.selectedComments.delete(key);
                });
                updateBatchDeleteButton();
                return;
            }
            if (t.classList.contains('row-check')) {
                const key = (t.dataset.platform || '') + '' + (t.dataset.commentId || '');
                if (t.checked) state.selectedComments.add(key);
                else state.selectedComments.delete(key);
                const boxes = [...document.querySelectorAll('.row-check')];
                const all = boxes.length > 0 && boxes.every((cb) => cb.checked);
                const head = $('check-all');
                if (head) head.checked = all;
                updateBatchDeleteButton();
            }
        });
        $('btn-batch-delete').addEventListener('click', batchDeleteComments);

        $('chat-send').addEventListener('click', sendAgentMessage);
        $('chat-input').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendAgentMessage();
            }
        });
        $('f-search').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { state.page = 1; render(); }
        });

        $('pagination').addEventListener('click', (e) => {
            const btn = e.target.closest('.page-btn');
            if (!btn || btn.disabled) return;
            const p = Number(btn.dataset.page);
            if (p >= 1) { state.page = p; render(); }
        });
    }

    bindEvents();
    loadData();
})();
