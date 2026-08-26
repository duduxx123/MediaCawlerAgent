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
            { key: 'commenter_id', label: '抖音号', type: 'mono' },
            { key: 'commenter_sec_uid', label: 'sec_uid', type: 'mono' },
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
        page: 1,
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
                    ? [row.comment, row.commenter_name, row.video_title, row.commenter_id].join(' ')
                    : [row.title, row.desc, row.nickname, row.creator_hash].join(' ');
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
        const columns = COLUMNS[state.tab];
        const filtered = filteredData();

        // 表头
        $('table-head').innerHTML = '<tr>' + columns.map((c) => `<th>${escapeHtml(c.label)}</th>`).join('') + '</tr>';

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
                '<tr>' + columns.map((c) => `<td>${renderCell(c, row)}</td>`).join('') + '</tr>'
            ).join('');
        }

        // 统计
        const allTotal = currentData().length;
        $('stats').innerHTML = total === allTotal
            ? `共 <b>${total}</b> 条记录`
            : `筛选结果 <b>${total}</b> 条 / 全部 ${allTotal} 条`;

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
            $('wordcloud-content').innerHTML = '<div class="wordcloud-empty">暂无词云文件。请先开启 <code>ENABLE_GET_WORDCLOUD = True</code>，并使用 JSON/JSONL 模式完成一次评论采集。</div>';
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

        const isWordcloud = tab === 'wordcloud';
        $('filters').hidden = isWordcloud;
        $('stats').hidden = isWordcloud;
        document.querySelector('.table-wrap').hidden = isWordcloud;
        $('pagination').hidden = isWordcloud;
        $('wordcloud-panel').hidden = !isWordcloud;

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
