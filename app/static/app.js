/**
 * LogsCrawler - Frontend Application
 * Docker Log Monitoring with AI-Powered Issue Detection
 */

// ==================== State ====================
const state = {
    containers: [],
    issues: [],
    logs: [],
    selectedContainer: 'all',
    logsPaused: false,
    logsAutoScroll: true,
    wsLogs: null,
    wsIssues: null,
    wsChat: null,
};

// ==================== API Helpers ====================
const API_BASE = '/api';

async function api(endpoint, options = {}) {
    try {
        const response = await fetch(`${API_BASE}${endpoint}`, {
            headers: {
                'Content-Type': 'application/json',
                ...options.headers,
            },
            ...options,
        });
        
        if (!response.ok) {
            throw new Error(`API Error: ${response.status}`);
        }
        
        return await response.json();
    } catch (error) {
        console.error('API Error:', error);
        throw error;
    }
}

// ==================== Initialization ====================
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    initWebSockets();
    checkStatus();
    loadDashboard();
    
    // Auto-refresh dashboard
    setInterval(refreshDashboard, 30000);
    
    // Initialize chat input
    const chatInput = document.getElementById('chat-input');
    if (chatInput) {
        chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendChatMessage();
            }
        });
        
        // Auto-resize textarea
        chatInput.addEventListener('input', () => {
            chatInput.style.height = 'auto';
            chatInput.style.height = Math.min(chatInput.scrollHeight, 150) + 'px';
        });
    }
    
    // Log search filter
    const logSearch = document.getElementById('logs-search');
    if (logSearch) {
        logSearch.addEventListener('input', filterLogs);
    }
    
    // Auto-scroll toggle
    const autoScrollCheck = document.getElementById('logs-autoscroll');
    if (autoScrollCheck) {
        autoScrollCheck.addEventListener('change', (e) => {
            state.logsAutoScroll = e.target.checked;
        });
    }
    
    // Container select for logs
    const logsSelect = document.getElementById('logs-container-select');
    if (logsSelect) {
        logsSelect.addEventListener('change', (e) => {
            state.selectedContainer = e.target.value;
            reconnectLogsWebSocket();
        });
    }
});

// ==================== Navigation ====================
function initNavigation() {
    const navItems = document.querySelectorAll('.nav-item');
    
    navItems.forEach(item => {
        item.addEventListener('click', () => {
            const viewId = item.dataset.view;
            switchView(viewId);
            
            // Update active state
            navItems.forEach(n => n.classList.remove('active'));
            item.classList.add('active');
        });
    });
}

function switchView(viewId) {
    const views = document.querySelectorAll('.view');
    views.forEach(view => view.classList.remove('active'));
    
    const targetView = document.getElementById(`${viewId}-view`);
    if (targetView) {
        targetView.classList.add('active');
    }
    
    // Load view-specific data
    switch (viewId) {
        case 'dashboard':
            loadDashboard();
            break;
        case 'containers':
            loadContainers();
            break;
        case 'logs':
            initLogsView();
            break;
        case 'ai':
            loadAIModels();
            break;
    }
}

// ==================== Status Check ====================
async function checkStatus() {
    try {
        const status = await api('/status');
        
        // Update Docker status
        const dockerStatus = document.getElementById('docker-status');
        if (status.docker.connected) {
            dockerStatus.classList.add('connected');
            dockerStatus.classList.remove('disconnected');
        } else {
            dockerStatus.classList.add('disconnected');
            dockerStatus.classList.remove('connected');
        }
        
        // Update AI status
        const aiStatus = document.getElementById('ai-status');
        if (status.ai.connected) {
            aiStatus.classList.add('connected');
            aiStatus.classList.remove('disconnected');
        } else {
            aiStatus.classList.add('disconnected');
            aiStatus.classList.remove('connected');
        }
        
        // Update AI model badge
        const modelBadge = document.getElementById('ai-model-badge');
        if (modelBadge) {
            modelBadge.textContent = `Model: ${status.ai.model}`;
        }
        
    } catch (error) {
        console.error('Failed to check status:', error);
    }
}

// ==================== Dashboard ====================
async function loadDashboard() {
    await Promise.all([
        loadContainersStats(),
        loadIssues(),
        loadRecentLogs()
    ]);
}

async function refreshDashboard() {
    await loadDashboard();
    showToast('Dashboard refreshed', 'info');
}

async function loadContainersStats() {
    try {
        const containers = await api('/containers');
        state.containers = containers;
        
        const total = containers.length;
        const running = containers.filter(c => c.status === 'running').length;
        
        document.getElementById('total-containers').textContent = total;
        document.getElementById('running-containers').textContent = running;
        
        // Update container selects
        updateContainerSelects(containers);
        
    } catch (error) {
        console.error('Failed to load containers:', error);
    }
}

function updateContainerSelects(containers) {
    const selects = [
        'dashboard-container-filter',
        'logs-container-select',
        'ai-container-select'
    ];
    
    selects.forEach(selectId => {
        const select = document.getElementById(selectId);
        if (select) {
            const currentValue = select.value;
            select.innerHTML = '<option value="all">All Containers</option>';
            
            containers.forEach(c => {
                const option = document.createElement('option');
                option.value = c.id;
                option.textContent = c.name;
                select.appendChild(option);
            });
            
            select.value = currentValue || 'all';
        }
    });
}

async function loadIssues() {
    try {
        // Show all issues with their occurrence count
        const issues = await api('/issues?limit=20&min_occurrences=1');
        state.issues = issues;
        
        const activeIssues = issues.filter(i => !i.resolved);
        const criticalIssues = issues.filter(i => i.severity === 'critical' && !i.resolved);
        
        document.getElementById('active-issues').textContent = activeIssues.length;
        document.getElementById('critical-issues').textContent = criticalIssues.length;
        
        // Apply current filter
        const filter = document.getElementById('issues-severity-filter')?.value || 'all';
        if (filter === 'all') {
            renderIssuesList(issues);
        } else {
            renderIssuesList(issues.filter(i => i.severity === filter));
        }
        
    } catch (error) {
        console.error('Failed to load issues:', error);
    }
}

function renderIssuesList(issues) {
    const container = document.getElementById('issues-list');
    
    if (!issues || issues.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
                    <polyline points="22 4 12 14.01 9 11.01"></polyline>
                </svg>
                <p>No issues detected</p>
                <span>All systems running smoothly</span>
            </div>
        `;
        return;
    }
    
    container.innerHTML = issues.map(issue => `
        <div class="issue-card ${issue.severity}" data-issue-id="${issue.id}">
            <div class="issue-header">
                <span class="issue-title">${escapeHtml(issue.title)}</span>
                <div class="issue-badges">
                    <span class="issue-badge occurrence" title="Occurred ${issue.occurrence_count} times">×${issue.occurrence_count}</span>
                    <span class="issue-badge ${issue.severity}">${issue.severity}</span>
                </div>
            </div>
            <div class="issue-container">Container: ${escapeHtml(issue.container_name)}</div>
            <div class="issue-description">${escapeHtml(issue.description.substring(0, 200))}${issue.description.length > 200 ? '...' : ''}</div>
            <div class="issue-excerpt">${escapeHtml(issue.log_excerpt.substring(0, 150))}</div>
            <div class="issue-footer">
                <span class="issue-time">${formatTime(issue.detected_at)}</span>
                <div class="issue-actions">
                    <button class="btn btn-secondary btn-sm" onclick="viewIssueLogs('${issue.id}')" title="View logs around this issue">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                            <polyline points="14 2 14 8 20 8"></polyline>
                            <line x1="16" y1="13" x2="8" y2="13"></line>
                            <line x1="16" y1="17" x2="8" y2="17"></line>
                        </svg>
                        Logs
                    </button>
                    <button class="btn btn-primary btn-sm" onclick="investigateIssue('${issue.id}')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                            <circle cx="12" cy="12" r="3"></circle>
                            <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"></path>
                        </svg>
                        AI
                    </button>
                    <button class="btn btn-ghost btn-sm" onclick="resolveIssue('${issue.id}')">Resolve</button>
                </div>
            </div>
        </div>
    `).join('');
}

async function loadRecentLogs() {
    try {
        const logs = await api('/logs?tail=30');
        renderDashboardLogs(logs);
    } catch (error) {
        console.error('Failed to load logs:', error);
    }
}

function renderDashboardLogs(logs) {
    const container = document.getElementById('dashboard-logs');
    
    if (!logs || logs.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <p>No recent logs</p>
            </div>
        `;
        return;
    }
    
    container.innerHTML = logs.slice(-50).map(log => {
        const level = detectLogLevel(log.message);
        return `
            <div class="log-entry ${level}">
                <span class="log-time">${formatLogTime(log.timestamp)}</span>
                <span class="log-container">${escapeHtml(log.container_name)}</span>
                <span class="log-message">${escapeHtml(log.message)}</span>
            </div>
        `;
    }).join('');
    
    container.scrollTop = container.scrollHeight;
}

// ==================== Issues Management ====================
async function scanForIssues() {
    try {
        showToast('Scanning logs for issues...', 'info');
        const result = await api('/issues/scan?log_lines=200', { method: 'POST' });
        
        if (result.issues_found > 0) {
            showToast(`Found ${result.issues_found} issue(s)`, 'warning');
        } else {
            showToast('No issues found', 'success');
        }
        
        await loadIssues();
    } catch (error) {
        showToast('Failed to scan for issues', 'error');
    }
}

async function resolveIssue(issueId) {
    try {
        await api(`/issues/${issueId}/resolve`, { method: 'POST' });
        showToast('Issue resolved', 'success');
        await loadIssues();
    } catch (error) {
        showToast('Failed to resolve issue', 'error');
    }
}

async function clearAllIssues() {
    try {
        const result = await api('/issues', { method: 'DELETE' });
        showToast(`Cleared ${result.count} issues`, 'success');
        await loadIssues();
    } catch (error) {
        showToast('Failed to clear issues', 'error');
    }
}

function filterIssuesBySeverity() {
    const filter = document.getElementById('issues-severity-filter').value;
    
    if (filter === 'all') {
        renderIssuesList(state.issues);
    } else {
        const filtered = state.issues.filter(i => i.severity === filter);
        renderIssuesList(filtered);
    }
}

async function viewIssueLogs(issueId) {
    const issue = state.issues.find(i => i.id === issueId);
    if (!issue) {
        showToast('Issue not found', 'error');
        return;
    }
    
    // Find container ID from name
    const container = state.containers.find(c => 
        c.name === issue.container_name || 
        issue.container_name.includes(c.name)
    );
    
    if (!container) {
        showToast('Container not found', 'error');
        return;
    }
    
    // Show modal with loading state
    showLogsModal(issue, null, true);
    
    try {
        // Fetch 100 lines of logs (approximately 50 before and 50 after)
        const logs = await api(`/logs/${container.id}?tail=100&timestamps=true`);
        showLogsModal(issue, logs, false);
    } catch (error) {
        console.error('Failed to fetch logs:', error);
        showToast('Failed to fetch logs', 'error');
        closeLogsModal();
    }
}

function showLogsModal(issue, logs, loading) {
    let modal = document.getElementById('logs-modal');
    
    if (!modal) {
        // Create modal if it doesn't exist
        modal = document.createElement('div');
        modal.id = 'logs-modal';
        modal.className = 'modal-overlay';
        document.body.appendChild(modal);
    }
    
    const logsContent = loading 
        ? '<div class="loading"><div class="spinner"></div></div>'
        : logs && logs.length > 0 
            ? logs.map(log => {
                const time = log.timestamp ? formatTime(log.timestamp) : '';
                const colorizedMessage = colorizeLogMessage(log.message);
                // Highlight lines that match the issue excerpt
                const isMatch = issue.log_excerpt && log.message.includes(issue.log_excerpt.substring(0, 50));
                // Determine log level for line styling
                const logLevel = detectLogLevel(log.message);
                return `<div class="modal-log-entry ${isMatch ? 'highlight' : ''} ${logLevel}">
                    <span class="log-time">${time}</span>
                    <span class="log-message">${colorizedMessage}</span>
                </div>`;
            }).join('')
            : '<div class="empty-state"><p>No logs found</p></div>';
    
    modal.innerHTML = `
        <div class="modal-content logs-modal-content">
            <div class="modal-header">
                <div class="modal-title">
                    <h3>Logs: ${escapeHtml(issue.container_name)}</h3>
                    <span class="modal-subtitle">${escapeHtml(issue.title)}</span>
                </div>
                <button class="btn btn-ghost modal-close" onclick="closeLogsModal()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
                        <line x1="18" y1="6" x2="6" y2="18"></line>
                        <line x1="6" y1="6" x2="18" y2="18"></line>
                    </svg>
                </button>
            </div>
            <div class="modal-body">
                <div class="modal-logs-viewer">
                    ${logsContent}
                </div>
            </div>
            <div class="modal-footer">
                <span class="modal-info">${logs ? logs.length : 0} log entries</span>
                <button class="btn btn-secondary" onclick="closeLogsModal()">Close</button>
            </div>
        </div>
    `;
    
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    
    // Close on backdrop click
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeLogsModal();
    });
    
    // Close on Escape key
    document.addEventListener('keydown', handleModalEscape);
}

function handleModalEscape(e) {
    if (e.key === 'Escape') closeLogsModal();
}

function closeLogsModal() {
    const modal = document.getElementById('logs-modal');
    if (modal) {
        modal.style.display = 'none';
        document.body.style.overflow = '';
        document.removeEventListener('keydown', handleModalEscape);
    }
}

function detectLogLevel(message) {
    const msgLower = message.toLowerCase();
    if (/\b(error|exception|fatal|panic|failed|failure)\b/i.test(message)) return 'level-error';
    if (/\b(warn|warning)\b/i.test(message)) return 'level-warning';
    if (/\b(debug)\b/i.test(message)) return 'level-debug';
    if (/\b(info)\b/i.test(message)) return 'level-info';
    return '';
}

function colorizeLogMessage(message) {
    // Escape HTML first
    let html = escapeHtml(message);
    
    // Log levels - ERROR, WARN, INFO, DEBUG (case insensitive, word boundaries)
    html = html.replace(/\b(ERROR|FATAL|PANIC|EXCEPTION)\b/gi, '<span class="log-error">$1</span>');
    html = html.replace(/\b(WARN|WARNING)\b/gi, '<span class="log-warn">$1</span>');
    html = html.replace(/\b(INFO)\b/gi, '<span class="log-info">$1</span>');
    html = html.replace(/\b(DEBUG|TRACE)\b/gi, '<span class="log-debug">$1</span>');
    
    // HTTP status codes
    html = html.replace(/\b([2]\d{2})\b/g, '<span class="log-status-success">$1</span>');  // 2xx
    html = html.replace(/\b([4]\d{2})\b/g, '<span class="log-status-warning">$1</span>');  // 4xx
    html = html.replace(/\b([5]\d{2})\b/g, '<span class="log-status-error">$1</span>');    // 5xx
    
    // HTTP methods
    html = html.replace(/\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b/g, '<span class="log-method">$1</span>');
    
    // URLs and paths (simplified)
    html = html.replace(/(https?:\/\/[^\s"'<>]+)/g, '<span class="log-url">$1</span>');
    html = html.replace(/("\/[^"]*")/g, '<span class="log-path">$1</span>');
    
    // IP addresses
    html = html.replace(/\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(:\d+)?\b/g, '<span class="log-ip">$1$2</span>');
    
    // Numbers with units (ms, s, bytes, etc.)
    html = html.replace(/\b(\d+\.?\d*)(ms|s|kb|mb|gb|bytes?)\b/gi, '<span class="log-number">$1$2</span>');
    
    // Quoted strings
    html = html.replace(/"([^"]*?)"/g, '<span class="log-string">"$1"</span>');
    html = html.replace(/'([^']*?)'/g, '<span class="log-string">\'$1\'</span>');
    
    // Brackets and braces for JSON-like content
    html = html.replace(/(\{|\}|\[|\])/g, '<span class="log-bracket">$1</span>');
    
    // Key-value pairs (key=value or key: value)
    html = html.replace(/\b([a-zA-Z_][a-zA-Z0-9_]*)=/g, '<span class="log-key">$1</span>=');
    
    // Timestamps in logs [HH:MM:SS] or similar
    html = html.replace(/\[(\d{2}:\d{2}:\d{2})\]/g, '[<span class="log-timestamp">$1</span>]');
    
    return html;
}

async function investigateIssue(issueId) {
    // Find the issue in state
    const issue = state.issues.find(i => i.id === issueId);
    if (!issue) {
        showToast('Issue not found', 'error');
        return;
    }
    
    // Switch to AI view
    switchView('ai');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector('[data-view="ai"]').classList.add('active');
    
    // Find container ID from name
    const container = state.containers.find(c => 
        c.name === issue.container_name || 
        issue.container_name.includes(c.name)
    );
    
    // Set container context if found
    const aiSelect = document.getElementById('ai-container-select');
    if (aiSelect && container) {
        aiSelect.value = container.id;
    } else if (aiSelect) {
        aiSelect.value = 'all';
    }
    
    // Build detailed investigation prompt
    const prompt = `I detected a ${issue.severity.toUpperCase()} issue in container "${issue.container_name}".

**Issue Title:** ${issue.title}
**Description:** ${issue.description}
**Log excerpt:** ${issue.log_excerpt}

Please help me understand:
1. Is this a serious error that needs immediate attention?
2. Does this error happen frequently or is it a one-time occurrence?
3. What is the root cause of this error?
4. How can I fix or resolve this issue?
5. Show me more context around this log entry if available.

Please analyze the recent logs from this container and provide detailed recommendations.`;
    
    // Send the prompt
    sendQuickPrompt(prompt);
}

// ==================== Containers View ====================
async function loadContainers() {
    try {
        const containers = await api('/containers?all=true');
        state.containers = containers;
        renderContainersGrid(containers);
        updateContainersCount();
        
        // Initialize search and filter if not already done
        initContainersSearch();
    } catch (error) {
        showToast('Failed to load containers', 'error');
    }
}

function initContainersSearch() {
    const searchInput = document.getElementById('containers-search');
    const statusFilter = document.getElementById('containers-status-filter');
    
    // Restore saved filter values from localStorage
    const savedSearch = localStorage.getItem('logscrawler_containers_search') || '';
    const savedStatus = localStorage.getItem('logscrawler_containers_status') || 'all';
    
    if (searchInput) {
        searchInput.value = savedSearch;
        if (!searchInput.dataset.initialized) {
            searchInput.addEventListener('input', filterContainers);
            searchInput.dataset.initialized = 'true';
        }
    }
    
    if (statusFilter) {
        statusFilter.value = savedStatus;
        if (!statusFilter.dataset.initialized) {
            statusFilter.addEventListener('change', filterContainers);
            statusFilter.dataset.initialized = 'true';
        }
    }
    
    // Apply filters if any were restored
    if (savedSearch || savedStatus !== 'all') {
        filterContainers();
    }
}

function filterContainers() {
    const searchInput = document.getElementById('containers-search');
    const statusFilter = document.getElementById('containers-status-filter');
    const searchTerm = (searchInput?.value || '').toLowerCase();
    const statusValue = statusFilter?.value || 'all';
    
    // Save filter values to localStorage
    localStorage.setItem('logscrawler_containers_search', searchInput?.value || '');
    localStorage.setItem('logscrawler_containers_status', statusValue);
    
    const cards = document.querySelectorAll('#containers-grid .container-card');
    let visibleCount = 0;
    
    cards.forEach(card => {
        const name = card.querySelector('.container-name')?.textContent.toLowerCase() || '';
        const image = card.querySelector('.container-image')?.textContent.toLowerCase() || '';
        const status = card.querySelector('.container-status-badge')?.textContent.toLowerCase() || '';
        
        const matchesSearch = name.includes(searchTerm) || image.includes(searchTerm);
        const matchesStatus = statusValue === 'all' || status.includes(statusValue);
        
        if (matchesSearch && matchesStatus) {
            card.classList.remove('hidden');
            visibleCount++;
        } else {
            card.classList.add('hidden');
        }
    });
    
    updateContainersCount(visibleCount);
}

function updateContainersCount(count) {
    const countEl = document.getElementById('containers-count');
    if (countEl) {
        const total = state.containers?.length || 0;
        if (count !== undefined && count !== total) {
            countEl.textContent = `${count} of ${total} containers`;
        } else {
            countEl.textContent = `${total} container${total !== 1 ? 's' : ''}`;
        }
    }
}

function renderContainersGrid(containers) {
    const grid = document.getElementById('containers-grid');
    
    if (!containers || containers.length === 0) {
        grid.innerHTML = `
            <div class="empty-state">
                <p>No containers found</p>
                <span>Make sure Docker is running</span>
            </div>
        `;
        return;
    }
    
    grid.innerHTML = containers.map(c => {
        const statusClass = c.status === 'running' ? 'running' : 
                           c.status === 'exited' ? 'stopped' : 'error';
        return `
            <div class="container-card" data-container-id="${c.id}" onclick="viewContainerLogs('${c.id}')">
                <div class="container-card-header">
                    <span class="container-name" title="${escapeHtml(c.name)}">
                        <span class="container-status-dot ${statusClass}"></span>
                        ${escapeHtml(c.name)}
                    </span>
                    <span class="container-status-badge ${statusClass}">${c.status}</span>
                </div>
                <div class="container-image" title="${escapeHtml(c.image)}">${escapeHtml(c.image)}</div>
                <div class="container-meta">
                    <span class="container-meta-item" title="ID: ${c.id}">
                        ID: ${c.id}
                    </span>
                    ${c.ports.length > 0 ? `
                        <span class="container-meta-item" title="${c.ports.join(', ')}">
                            ${c.ports.slice(0, 2).join(', ')}${c.ports.length > 2 ? '...' : ''}
                        </span>
                    ` : ''}
                </div>
                <div class="container-actions">
                    <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); viewContainerLogs('${c.id}')" title="View Logs">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                            <polyline points="14 2 14 8 20 8"></polyline>
                        </svg>
                        Logs
                    </button>
                    <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); analyzeContainer('${c.id}')" title="Analyze with AI">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <circle cx="12" cy="12" r="3"></circle>
                            <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"></path>
                        </svg>
                        AI
                    </button>
                </div>
            </div>
        `;
    }).join('');
}

function viewContainerLogs(containerId) {
    state.selectedContainer = containerId;
    
    // Update select
    const select = document.getElementById('logs-container-select');
    if (select) select.value = containerId;
    
    // Switch to logs view
    switchView('logs');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector('[data-view="logs"]').classList.add('active');
    
    reconnectLogsWebSocket();
}

async function analyzeContainer(containerId) {
    const container = state.containers.find(c => c.id === containerId);
    const name = container ? container.name : containerId;
    
    // Switch to AI view
    switchView('ai');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector('[data-view="ai"]').classList.add('active');
    
    // Set container context
    const aiSelect = document.getElementById('ai-container-select');
    if (aiSelect) aiSelect.value = containerId;
    
    // Send analysis prompt
    sendQuickPrompt(`Analyze the logs from container "${name}" and tell me if there are any issues or problems.`);
}

// ==================== Logs View ====================
function initLogsView() {
    loadContainerSelects();
    reconnectLogsWebSocket();
}

function loadContainerSelects() {
    if (state.containers.length > 0) {
        updateContainerSelects(state.containers);
    }
}

function reconnectLogsWebSocket() {
    // Close existing connection
    if (state.wsLogs) {
        state.wsLogs.close();
    }
    
    // Clear logs
    const viewer = document.getElementById('log-viewer');
    if (viewer) {
        viewer.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    }
    state.logs = [];
    
    // Connect to appropriate endpoint
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const endpoint = state.selectedContainer === 'all' 
        ? '/ws/logs' 
        : `/ws/logs/${state.selectedContainer}`;
    
    state.wsLogs = new WebSocket(`${wsProtocol}//${window.location.host}${endpoint}`);
    
    state.wsLogs.onopen = () => {
        console.log('Logs WebSocket connected');
        if (viewer) viewer.innerHTML = '';
    };
    
    state.wsLogs.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        if (data.type === 'log' && !state.logsPaused) {
            addLogEntry(data.data);
        }
    };
    
    state.wsLogs.onclose = () => {
        console.log('Logs WebSocket closed');
    };
    
    state.wsLogs.onerror = (error) => {
        console.error('Logs WebSocket error:', error);
    };
}

function addLogEntry(log) {
    state.logs.push(log);
    
    // Keep only last 1000 logs
    if (state.logs.length > 1000) {
        state.logs.shift();
    }
    
    const viewer = document.getElementById('log-viewer');
    if (!viewer) return;
    
    const level = detectLogLevel(log.message);
    const entry = document.createElement('div');
    entry.className = `log-entry ${level}`;
    entry.innerHTML = `
        <span class="log-time">${formatLogTime(log.timestamp)}</span>
        <span class="log-container">${escapeHtml(log.container_name)}</span>
        <span class="log-message">${escapeHtml(log.message)}</span>
    `;
    
    viewer.appendChild(entry);
    
    // Auto-scroll
    if (state.logsAutoScroll) {
        viewer.scrollTop = viewer.scrollHeight;
    }
    
    // Remove old entries from DOM
    while (viewer.children.length > 1000) {
        viewer.removeChild(viewer.firstChild);
    }
}

function toggleLogsPause() {
    state.logsPaused = !state.logsPaused;
    const btn = document.getElementById('logs-pause-btn');
    
    if (state.logsPaused) {
        btn.innerHTML = `
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polygon points="5 3 19 12 5 21 5 3"></polygon>
            </svg>
            Resume
        `;
    } else {
        btn.innerHTML = `
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="6" y="4" width="4" height="16"></rect>
                <rect x="14" y="4" width="4" height="16"></rect>
            </svg>
            Pause
        `;
    }
}

function clearLogs() {
    state.logs = [];
    const viewer = document.getElementById('log-viewer');
    if (viewer) viewer.innerHTML = '';
}

function filterLogs() {
    const searchTerm = document.getElementById('logs-search').value.toLowerCase();
    const entries = document.querySelectorAll('#log-viewer .log-entry');
    
    entries.forEach(entry => {
        const text = entry.textContent.toLowerCase();
        entry.style.display = text.includes(searchTerm) ? '' : 'none';
    });
}

// ==================== WebSockets ====================
function initWebSockets() {
    // Issues WebSocket
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    
    state.wsIssues = new WebSocket(`${wsProtocol}//${window.location.host}/ws/issues`);
    
    state.wsIssues.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        if (data.type === 'new_issue') {
            showToast(`New ${data.data.severity} issue in ${data.data.container_name}`, 
                data.data.severity === 'critical' ? 'error' : 'warning');
            loadIssues();
        }
    };
    
    state.wsIssues.onclose = () => {
        // Reconnect after delay
        setTimeout(() => {
            if (state.wsIssues.readyState === WebSocket.CLOSED) {
                initWebSockets();
            }
        }, 5000);
    };
}

// ==================== AI Chat ====================
async function loadAIModels() {
    try {
        const data = await api('/ai/models');
        const badge = document.getElementById('ai-model-badge');
        if (badge) {
            badge.textContent = `Model: ${data.current_model}`;
        }
    } catch (error) {
        console.error('Failed to load AI models:', error);
    }
}

async function sendChatMessage() {
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    
    if (!message) return;
    
    // Clear input
    input.value = '';
    input.style.height = 'auto';
    
    // Hide welcome
    const welcome = document.querySelector('.chat-welcome');
    if (welcome) welcome.style.display = 'none';
    
    // Add user message
    addChatMessage('user', message);
    
    // Get context settings
    const includeLogs = document.getElementById('include-logs-context').checked;
    const containerId = document.getElementById('ai-container-select').value;
    
    try {
        // Add loading indicator
        const loadingId = addChatMessage('assistant', '<div class="loading"><div class="spinner"></div></div>', true);
        
        const params = new URLSearchParams({
            message: message,
            include_logs: includeLogs,
            log_lines: 100,
        });
        
        if (containerId !== 'all') {
            params.append('container_id', containerId);
        }
        
        const response = await api(`/ai/chat?${params.toString()}`, { method: 'POST' });
        
        // Update message
        updateChatMessage(loadingId, response.response);
        
    } catch (error) {
        showToast('Failed to get AI response', 'error');
    }
}

function sendQuickPrompt(prompt) {
    const input = document.getElementById('chat-input');
    input.value = prompt;
    sendChatMessage();
}

function addChatMessage(role, content, isLoading = false) {
    const container = document.getElementById('chat-messages');
    const id = 'msg-' + Date.now();
    
    const messageDiv = document.createElement('div');
    messageDiv.className = `chat-message ${role}`;
    messageDiv.id = id;
    messageDiv.innerHTML = `
        <div class="message-content">${isLoading ? content : escapeHtml(content)}</div>
    `;
    
    container.appendChild(messageDiv);
    container.scrollTop = container.scrollHeight;
    
    return id;
}

function updateChatMessage(id, content) {
    const message = document.getElementById(id);
    if (message) {
        const contentDiv = message.querySelector('.message-content');
        contentDiv.innerHTML = formatAIResponse(content);
    }
}

function formatAIResponse(text) {
    // Convert markdown-like formatting
    let formatted = escapeHtml(text);
    
    // Code blocks
    formatted = formatted.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    formatted = formatted.replace(/`([^`]+)`/g, '<code>$1</code>');
    
    // Bold
    formatted = formatted.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    
    // Newlines
    formatted = formatted.replace(/\n/g, '<br>');
    
    return formatted;
}

// ==================== Utilities ====================
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatTime(isoString) {
    if (!isoString) return 'Unknown';
    const date = new Date(isoString);
    return date.toLocaleString();
}

function formatLogTime(isoString) {
    if (!isoString) return '--:--:--';
    const date = new Date(isoString);
    return date.toLocaleTimeString();
}

function detectLogLevel(message) {
    if (!message) return '';
    const lower = message.toLowerCase();
    
    if (lower.includes('error') || lower.includes('exception') || 
        lower.includes('fatal') || lower.includes('failed')) {
        return 'error';
    }
    if (lower.includes('warn') || lower.includes('warning')) {
        return 'warning';
    }
    return '';
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    const icons = {
        success: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline>',
        error: '<circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line>',
        warning: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line>',
        info: '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line>',
    };
    
    toast.innerHTML = `
        <svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            ${icons[type] || icons.info}
        </svg>
        <span class="toast-message">${escapeHtml(message)}</span>
    `;
    
    container.appendChild(toast);
    
    // Auto-remove after 4 seconds
    setTimeout(() => {
        toast.style.animation = 'slideIn 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}
