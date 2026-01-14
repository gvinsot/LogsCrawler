/**
 * LogsCrawler - Frontend Application
 * Docker Log Monitoring with AI-Powered Issue Detection
 */

// ==================== State ====================
const state = {
    containers: [],
    issues: [],
    logs: [],
    systems: [],  // Remote systems
    selectedContainer: 'all',
    selectedSystem: 'all',  // System filter
    aiSelectedSystemId: null,  // System for AI context (set by analyzeContainer)
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
        case 'systems':
            loadSystems();
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
            
            // For logs-container-select, only show local containers (WebSocket streaming only works locally)
            const filteredContainers = selectId === 'logs-container-select'
                ? containers.filter(c => !c.system_id || c.system_id === 'local')
                : containers;
            
            filteredContainers.forEach(c => {
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
        
        // Restore saved filter from localStorage
        const severityFilter = document.getElementById('issues-severity-filter');
        const savedSeverity = localStorage.getItem('logscrawler_issues_severity') || 'all';
        if (severityFilter && severityFilter.value !== savedSeverity) {
            severityFilter.value = savedSeverity;
        }
        
        // Apply current filter using severity hierarchy
        applyIssuesSeverityFilter();
        
    } catch (error) {
        console.error('Failed to load issues:', error);
    }
}

function applyIssuesSeverityFilter() {
    const filter = document.getElementById('issues-severity-filter')?.value || 'all';
    
    // Severity hierarchy: info < warning < error < critical
    // Show selected level and all higher levels
    const severityLevels = {
        'info': 0,
        'warning': 1,
        'error': 2,
        'critical': 3
    };
    
    let filteredIssues;
    if (filter === 'all') {
        filteredIssues = state.issues;
    } else {
        const minLevel = severityLevels[filter] || 0;
        filteredIssues = state.issues.filter(i => (severityLevels[i.severity] || 0) >= minLevel);
    }
    
    renderIssuesList(filteredIssues);
    
    // Also restore and apply system filter
    const systemFilter = document.getElementById('issues-system-filter');
    const savedSystem = localStorage.getItem('logscrawler_issues_system') || 'all';
    if (systemFilter && systemFilter.value !== savedSystem) {
        systemFilter.value = savedSystem;
    }
    
    // Apply system filter to rendered issues
    if (savedSystem !== 'all') {
        const issueCards = document.querySelectorAll('#issues-list .issue-card');
        issueCards.forEach(card => {
            const issueSystem = card.dataset.systemId || 'local';
            if (issueSystem !== savedSystem) {
                card.style.display = 'none';
            }
        });
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
        <div class="issue-card ${issue.severity}" data-issue-id="${issue.id}" data-system-id="${issue.system_id || 'local'}">
            <div class="issue-header">
                <span class="issue-title">${escapeHtml(issue.title)}</span>
                <div class="issue-badges">
                    <span class="issue-badge occurrence" title="Occurred ${issue.occurrence_count} times">×${issue.occurrence_count}</span>
                    <span class="issue-badge ${issue.severity}">${issue.severity}</span>
                    <span class="issue-badge system" title="System">${escapeHtml(issue.system_name || 'Local')}</span>
                </div>
            </div>
            <div class="issue-container">Container: ${escapeHtml(issue.container_name)}</div>
            <div class="issue-description">${escapeHtml(issue.description.substring(0, 200))}${issue.description.length > 200 ? '...' : ''}</div>
            <div class="issue-excerpt">${escapeHtml(issue.log_excerpt)}</div>
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
        const result = await api('/issues/scan?log_lines=500', { method: 'POST' });
        
        const status = result.analysis_status;
        const isInitialScan = !status?.initial_scan_done || status?.total_logs_analyzed <= result.logs_scanned;
        
        if (result.issues_found > 0) {
            showToast(`Found ${result.issues_found} issue(s) in ${result.logs_scanned} logs`, 'warning');
        } else if (isInitialScan) {
            showToast(`Initial scan complete: ${result.logs_scanned} logs analyzed`, 'success');
        } else {
            showToast('No new issues found', 'success');
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
    
    // Save to localStorage for persistence
    localStorage.setItem('logscrawler_issues_severity', filter);
    
    // Apply filter using shared function
    applyIssuesSeverityFilter();
}

async function viewIssueLogs(issueId) {
    const issue = state.issues.find(i => i.id === issueId);
    if (!issue) {
        showToast('Issue not found', 'error');
        return;
    }
    
    // Use the container_id directly from the issue (it's already stored)
    // Fall back to name lookup only if container_id is missing
    let containerId = issue.container_id;
    if (!containerId) {
        const container = state.containers.find(c => 
            c.name === issue.container_name || 
            issue.container_name.includes(c.name)
        );
        if (!container) {
            showToast('Container not found', 'error');
            return;
        }
        containerId = container.id;
    }
    
    // Show modal with loading state
    showLogsModal(issue, null, true, null);
    
    try {
        // Use the timestamp-based endpoint to find logs around when the issue was detected
        // This is more accurate than text search as it uses the actual detection time
        const timestamp = encodeURIComponent(issue.detected_at);
        const searchText = encodeURIComponent(issue.log_excerpt || '');
        const result = await api(`/logs/${containerId}/by-time?timestamp=${timestamp}&search=${searchText}&context_before=50&context_after=50&max_logs=20000`);
        
        if (result.found) {
            showLogsModal(issue, result.logs, false, result.match_index);
        } else {
            // Not found by time, show what we have with a warning
            showToast('Could not find exact log entry, showing logs around detection time', 'warning');
            showLogsModal(issue, result.logs, false, null);
        }
    } catch (error) {
        console.error('Failed to fetch logs:', error);
        showToast('Failed to fetch logs', 'error');
        closeLogsModal();
    }
}

function showLogsModal(issue, logs, loading, matchIndex = null) {
    console.log('[Debug] showLogsModal called:', {
        issueId: issue?.id,
        issueTitle: issue?.title,
        logsCount: logs?.length,
        loading,
        matchIndex,
        logExcerpt: issue?.log_excerpt?.substring(0, 100)
    });
    
    let modal = document.getElementById('logs-modal');
    
    if (!modal) {
        // Create modal if it doesn't exist
        modal = document.createElement('div');
        modal.id = 'logs-modal';
        modal.className = 'modal-overlay';
        document.body.appendChild(modal);
    }
    
    // Determine which line to highlight
    let highlightIndex = matchIndex;
    
    // If server didn't find a match, try client-side text search
    if ((matchIndex === null || matchIndex === -1 || matchIndex === undefined) && logs && logs.length > 0 && issue.log_excerpt) {
        console.log('[Debug] Server match not found, trying client-side search');
        const excerpt = issue.log_excerpt;
        
        // Try multiple search strategies
        const searchAttempts = [];
        
        // HIGHEST PRIORITY: Extract UUIDs - these are unique identifiers
        const uuidPattern = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
        const uuids = excerpt.match(uuidPattern);
        if (uuids) searchAttempts.push(...uuids);
        
        // Extract connection IDs
        const connIds = excerpt.match(/conn(\d+)/gi);
        if (connIds) searchAttempts.push(...connIds);
        
        // Extract IP:port combinations
        const ipPorts = excerpt.match(/\d+\.\d+\.\d+\.\d+:\d+/g);
        if (ipPorts) searchAttempts.push(...ipPorts);
        
        // Extract key patterns from the excerpt
        const msgMatch = excerpt.match(/"msg":\s*"([^"]+)"/);
        if (msgMatch) searchAttempts.push(msgMatch[1]);
        
        const errorMatch = excerpt.match(/ERROR[:\s]+([^\n]+)/i);
        if (errorMatch) searchAttempts.push(errorMatch[1].substring(0, 50));
        
        const warningMatch = excerpt.match(/WARNING[:\s]+([^\n]+)/i);
        if (warningMatch) searchAttempts.push(warningMatch[1].substring(0, 50));
        
        // Extract INFO/DEBUG/CRITICAL messages
        const infoMatch = excerpt.match(/INFO\s*[-:]\s*(.{15,})/i);
        if (infoMatch) searchAttempts.push(infoMatch[1].substring(0, 60));
        
        const debugMatch = excerpt.match(/DEBUG\s*[-:]\s*(.{15,})/i);
        if (debugMatch) searchAttempts.push(debugMatch[1].substring(0, 60));
        
        const criticalMatch = excerpt.match(/CRITICAL\s*[-:]\s*(.{15,})/i);
        if (criticalMatch) searchAttempts.push(criticalMatch[1].substring(0, 60));
        
        // Split by "..." and try each part (for truncated excerpts)
        const parts = excerpt.split('...');
        for (const part of parts) {
            const trimmed = part.trim();
            if (trimmed.length > 15 && !trimmed.startsWith('[')) {
                searchAttempts.push(trimmed.substring(0, 80));
            }
        }
        
        // Search for matches
        for (const attempt of searchAttempts) {
            if (!attempt || attempt.length < 3) continue;
            for (let i = 0; i < logs.length; i++) {
                if (logs[i].message && logs[i].message.includes(attempt)) {
                    console.log('[Debug] Client-side match found:', { 
                        attempt, 
                        index: i, 
                        logMessage: logs[i].message.substring(0, 100) 
                    });
                    highlightIndex = i;
                    break;
                }
            }
            if (highlightIndex >= 0) break;
        }
        
        if (highlightIndex < 0) {
            console.log('[Debug] No client-side match found. Search attempts were:', searchAttempts);
        }
    }
    
    console.log('[Debug] Final highlightIndex:', highlightIndex, 'Total logs:', logs?.length || 0);
    
    const logsContent = loading 
        ? '<div class="loading"><div class="spinner"></div></div>'
        : logs && logs.length > 0 
            ? logs.map((log, index) => {
                const time = log.timestamp ? formatTime(log.timestamp) : '';
                const colorizedMessage = colorizeLogMessage(log.message);
                // Highlight the matched line
                const isMatch = highlightIndex !== null && highlightIndex >= 0 && index === highlightIndex;
                // Determine log level for line styling
                const logLevel = detectLogLevel(log.message);
                return `<div class="modal-log-entry ${isMatch ? 'highlight' : ''} ${logLevel}" ${isMatch ? 'data-match="true"' : ''}>
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
                </div>
                <button class="btn btn-ghost modal-close" onclick="closeLogsModal()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
                        <line x1="18" y1="6" x2="6" y2="18"></line>
                        <line x1="6" y1="6" x2="18" y2="18"></line>
                    </svg>
                </button>
            </div>
            <div class="modal-issue-info">
                <div class="modal-issue-header">
                    <div class="modal-issue-title-row">
                        <span class="issue-badge ${issue.severity}">${issue.severity.toUpperCase()}</span>
                        <span class="issue-badge occurrence">×${issue.occurrence_count || 1}</span>
                        <h4>${escapeHtml(issue.title)}</h4>
                    </div>
                    <div class="modal-issue-actions">
                        <button class="btn btn-primary btn-sm" onclick="closeLogsModal(); investigateIssue('${issue.id}')">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                                <circle cx="12" cy="12" r="3"></circle>
                                <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"></path>
                            </svg>
                            Analyze with AI
                        </button>
                        <button class="btn btn-secondary btn-sm" onclick="closeLogsModal(); resolveIssue('${issue.id}')">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                                <polyline points="20 6 9 17 4 12"></polyline>
                            </svg>
                            Resolve
                        </button>
                    </div>
                </div>
                <div class="modal-issue-description">${escapeHtml(issue.description)}</div>
                <div class="modal-issue-excerpt">
                    <strong>Detected log:</strong> <code>${escapeHtml(issue.log_excerpt)}</code>
                </div>
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
    
    // Auto-scroll to highlighted line within the logs viewer
    setTimeout(() => {
        const logsViewer = modal.querySelector('.modal-logs-viewer');
        // Find the matched line (prioritize data-match attribute)
        const highlightedLine = modal.querySelector('.modal-log-entry[data-match="true"]') || 
                                modal.querySelector('.modal-log-entry.highlight');
        
        console.log('[Debug] Scroll Logic:', {
            viewerExists: !!logsViewer,
            highlightedLineExists: !!highlightedLine,
            highlightedLineIndex: highlightedLine ? Array.from(logsViewer.querySelectorAll('.modal-log-entry')).indexOf(highlightedLine) : -1,
            totalLogs: logsViewer ? logsViewer.querySelectorAll('.modal-log-entry').length : 0
        });
        
        if (logsViewer && highlightedLine) {
            // Use offsetTop for accurate positioning within the scroll container
            const lineOffsetTop = highlightedLine.offsetTop;
            const viewerHeight = logsViewer.clientHeight;
            const lineHeight = highlightedLine.clientHeight;
            
            // Calculate scroll position to center the highlighted line
            const scrollTarget = lineOffsetTop - (viewerHeight / 2) + (lineHeight / 2);
            
            console.log('[Debug] Scroll Calculation:', {
                lineOffsetTop,
                viewerHeight,
                lineHeight,
                scrollTarget,
                currentScrollTop: logsViewer.scrollTop
            });
            
            logsViewer.scrollTo({ top: Math.max(0, scrollTarget), behavior: 'smooth' });
        }
    }, 300);
    
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
    
    // Only highlight log levels - keep it simple and readable
    html = html.replace(/\b(ERROR|FATAL|PANIC|EXCEPTION|FAILED|FAILURE)\b/gi, '<span class="log-error">$1</span>');
    html = html.replace(/\b(WARN|WARNING)\b/gi, '<span class="log-warn">$1</span>');
    html = html.replace(/\b(INFO)\b/gi, '<span class="log-info">$1</span>');
    html = html.replace(/\b(DEBUG|TRACE)\b/gi, '<span class="log-debug">$1</span>');
    
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
    
    // Use the container_id directly from the issue
    let containerId = issue.container_id;
    const container = containerId 
        ? state.containers.find(c => c.id === containerId)
        : state.containers.find(c => c.name === issue.container_name || issue.container_name.includes(c.name));
    
    if (!containerId && container) {
        containerId = container.id;
    }
    
    // Set container context if found
    const aiSelect = document.getElementById('ai-container-select');
    if (aiSelect && containerId) {
        aiSelect.value = containerId;
    } else if (aiSelect) {
        aiSelect.value = 'all';
    }
    
    // Fetch logs to get context around the issue using the timestamp-based endpoint
    let logContext = issue.log_excerpt;
    if (containerId) {
        try {
            const timestamp = encodeURIComponent(issue.detected_at);
            const searchText = encodeURIComponent(issue.log_excerpt || '');
            const result = await api(`/logs/${containerId}/by-time?timestamp=${timestamp}&search=${searchText}&context_before=1&context_after=5&max_logs=20000`);
            
            if (result.found && result.logs.length > 0) {
                const matchIndex = result.match_index;
                logContext = result.logs.map((log, idx) => {
                    const time = log.timestamp ? formatTime(log.timestamp) : '';
                    const marker = (idx === matchIndex) ? '>>> ' : '    ';
                    return `${marker}[${time}] ${log.message}`;
                }).join('\n');
            }
        } catch (error) {
            console.error('Failed to fetch logs for AI context:', error);
        }
    }
    
    // Build detailed investigation prompt with log context
    const prompt = `I detected a ${issue.severity.toUpperCase()} issue in container "${issue.container_name}".

**Issue Title:** ${issue.title}
**Description:** ${issue.description}

**Log context (line before, issue line marked with >>>, and 5 lines after):**
\`\`\`
${logContext}
\`\`\`

Please help me understand:
1. Is this a serious error that needs immediate attention?
2. What is the root cause of this error based on the log context?
3. How can I fix or resolve this issue?
4. Are there any related issues visible in the surrounding logs?

Please provide detailed recommendations.`;
    
    // Send the prompt
    sendQuickPrompt(prompt);
}

// ==================== Containers View ====================
async function loadContainers() {
    try {
        // First load remote systems to populate filters
        if (state.systems.length === 0) {
            try {
                const systems = await api('/systems');
                state.systems = systems;
                updateSystemFilters();
            } catch (e) {
                // Ignore errors - remote systems may not be available
            }
        }
        
        // Default to 'local' system if not already set to a specific value
        if (state.selectedSystem === 'all') {
            state.selectedSystem = 'local';
            const systemFilter = document.getElementById('containers-system-filter');
            if (systemFilter) systemFilter.value = 'local';
        }
        
        // Load containers based on selected system filter
        await loadAllContainers();
        
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
        const name = (card.querySelector('.container-name')?.textContent.toLowerCase() || '') + 
                     (card.dataset.containerName?.toLowerCase() || '');
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
    
    // Hide empty groups
    const groups = document.querySelectorAll('#containers-grid .container-group');
    groups.forEach(group => {
        const visibleCards = group.querySelectorAll('.container-card:not(.hidden)');
        if (visibleCards.length === 0) {
            group.classList.add('hidden');
        } else {
            group.classList.remove('hidden');
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
    
    // Group containers by docker-compose project
    const groups = {};
    containers.forEach(c => {
        const project = c.labels?.['com.docker.compose.project'] || 'Other Containers';
        if (!groups[project]) {
            groups[project] = [];
        }
        groups[project].push(c);
    });
    
    // Sort groups: docker-compose projects first (alphabetically), then "Other Containers" last
    const sortedGroupNames = Object.keys(groups).sort((a, b) => {
        if (a === 'Other Containers') return 1;
        if (b === 'Other Containers') return -1;
        return a.localeCompare(b);
    });
    
    // Render grouped containers
    let html = '';
    sortedGroupNames.forEach(groupName => {
        const groupContainers = groups[groupName];
        const runningCount = groupContainers.filter(c => c.status === 'running').length;
        
        html += `
            <div class="container-group">
                <div class="container-group-header">
                    <div class="container-group-title">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
                            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
                        </svg>
                        <span>${escapeHtml(groupName)}</span>
                    </div>
                    <span class="container-group-count">${runningCount}/${groupContainers.length} running</span>
                </div>
                <div class="container-group-grid">
        `;
        
        groupContainers.forEach(c => {
            const statusClass = c.status === 'running' ? 'running' : 
                               c.status === 'exited' ? 'stopped' : 'error';
            const serviceName = c.labels?.['com.docker.compose.service'] || c.name;
            
            html += `
                <div class="container-card" data-container-id="${c.id}" data-container-name="${escapeHtml(c.name)}" data-system-id="${c.system_id || 'local'}" onclick="viewContainerLogs('${c.id}', '${c.system_id || 'local'}')">
                    <div class="container-card-header">
                        <span class="container-name" title="${escapeHtml(c.name)}">
                            <span class="container-status-dot ${statusClass}"></span>
                            ${escapeHtml(serviceName)}
                        </span>
                        <span class="container-status-badge ${statusClass}">${c.status}</span>
                    </div>
                    <div class="container-image" title="${escapeHtml(c.image)}">${escapeHtml(c.image)}</div>
                    ${c.system_id && c.system_id !== 'local' ? `
                        <div class="container-system">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                                <rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect>
                                <line x1="6" y1="6" x2="6.01" y2="6"></line>
                            </svg>
                            ${escapeHtml(c.system_name || 'Remote')}
                        </div>
                    ` : ''}
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
                        <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); viewContainerLogs('${c.id}', '${c.system_id || 'local'}')" title="View Logs">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                                <polyline points="14 2 14 8 20 8"></polyline>
                            </svg>
                            Logs
                        </button>
                        <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); analyzeContainer('${c.id}', '${c.system_id || 'local'}')" title="Analyze with AI">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <circle cx="12" cy="12" r="3"></circle>
                                <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"></path>
                            </svg>
                            AI
                        </button>
                    </div>
                </div>
            `;
        });
        
        html += `
                </div>
            </div>
        `;
    });
    
    grid.innerHTML = html;
}

function viewContainerLogs(containerId, systemId = 'local') {
    state.selectedContainer = containerId;
    state.selectedSystem = systemId;
    
    // For remote systems, show logs in a different way (static fetch instead of WebSocket)
    if (systemId && systemId !== 'local') {
        showRemoteContainerLogs(containerId, systemId);
        return;
    }
    
    // Update select
    const select = document.getElementById('logs-container-select');
    if (select) select.value = containerId;
    
    // Switch to logs view
    switchView('logs');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector('[data-view="logs"]').classList.add('active');
    
    reconnectLogsWebSocket();
}

async function showRemoteContainerLogs(containerId, systemId) {
    const system = state.systems.find(s => s.id === systemId);
    const systemName = system ? system.name : 'Remote';
    
    showToast(`Loading logs from ${systemName}...`, 'info');
    
    try {
        const logs = await api(`/systems/${systemId}/logs/${containerId}?tail=200&timestamps=true`);
        
        // Show logs in a modal similar to issue logs
        const container = state.containers.find(c => c.id === containerId);
        const containerName = container ? container.name : containerId;
        
        showRemoteLogsModal(containerName, systemName, logs);
        
    } catch (error) {
        console.error('Failed to load remote logs:', error);
        showToast('Failed to load remote logs', 'error');
    }
}

function showRemoteLogsModal(containerName, systemName, logs) {
    let modal = document.getElementById('remote-logs-modal');
    
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'remote-logs-modal';
        modal.className = 'modal-overlay';
        document.body.appendChild(modal);
    }
    
    const logsHtml = logs.map(log => {
        const time = log.timestamp ? formatTime(log.timestamp) : '';
        const logLevel = detectRemoteLogLevel(log.message);
        const colorizedMessage = colorizeRemoteLogMessage(log.message);
        return `<div class="modal-log-entry ${logLevel}">
            <span class="log-time">${time}</span>
            <span class="log-message">${colorizedMessage}</span>
        </div>`;
    }).join('');
    
    modal.innerHTML = `
        <div class="logs-modal-content">
            <div class="modal-header">
                <div class="modal-title-section">
                    <h2>Container Logs: ${escapeHtml(containerName)}</h2>
                    <span class="modal-subtitle">System: ${escapeHtml(systemName)}</span>
                </div>
                <button class="btn btn-ghost btn-close" onclick="closeRemoteLogsModal()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="18" y1="6" x2="6" y2="18"></line>
                        <line x1="6" y1="6" x2="18" y2="18"></line>
                    </svg>
                </button>
            </div>
            <div class="modal-body">
                <div class="modal-logs-viewer">
                    ${logsHtml || '<div class="empty-state"><p>No logs available</p></div>'}
                </div>
            </div>
        </div>
    `;
    
    modal.style.display = 'flex';
    modal.onclick = (e) => {
        if (e.target === modal) closeRemoteLogsModal();
    };
}

function closeRemoteLogsModal() {
    const modal = document.getElementById('remote-logs-modal');
    if (modal) modal.style.display = 'none';
}

function detectRemoteLogLevel(message) {
    if (!message) return '';
    const lower = message.toLowerCase();
    
    // Error patterns - red highlighting
    if (/\b(error|exception|fatal|panic|failed|failure|critical)\b/i.test(message)) {
        return 'level-error';
    }
    // Warning patterns - orange highlighting
    if (/\b(warn|warning)\b/i.test(message)) {
        return 'level-warning';
    }
    return '';
}

function colorizeRemoteLogMessage(message) {
    // Escape HTML first
    let html = escapeHtml(message);
    
    // Highlight error keywords in red
    html = html.replace(/\b(ERROR|FATAL|PANIC|EXCEPTION|FAILED|FAILURE|CRITICAL)\b/gi, '<span class="log-error">$1</span>');
    // Highlight warning keywords in orange
    html = html.replace(/\b(WARN|WARNING)\b/gi, '<span class="log-warn">$1</span>');
    
    return html;
}

async function analyzeContainer(containerId, systemId = 'local') {
    const container = state.containers.find(c => c.id === containerId);
    const name = container ? container.name : containerId;
    const effectiveSystemId = systemId || (container ? container.system_id : 'local') || 'local';
    
    // Switch to AI view
    switchView('ai');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector('[data-view="ai"]').classList.add('active');
    
    // Set container context
    const aiSelect = document.getElementById('ai-container-select');
    if (aiSelect) aiSelect.value = containerId;
    
    // Store the system_id for AI context
    state.aiSelectedSystemId = effectiveSystemId;
    
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
    
    // Determine system_id from the selected container or from analyzeContainer call
    let systemId = state.aiSelectedSystemId || 'local';
    if (containerId && containerId !== 'all') {
        const container = state.containers.find(c => c.id === containerId);
        if (container && container.system_id) {
            systemId = container.system_id;
        }
    }
    
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
        
        // Include system_id for remote containers
        if (systemId && systemId !== 'local') {
            params.append('system_id', systemId);
        }
        
        const response = await api(`/ai/chat?${params.toString()}`, { method: 'POST' });
        
        // Update message
        updateChatMessage(loadingId, response.response);
        
        // Clear the AI system context after use
        state.aiSelectedSystemId = null;
        
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
    // Use 24h format: YYYY-MM-DD HH:MM:SS
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const hours = String(date.getHours()).padStart(2, '0');
    const minutes = String(date.getMinutes()).padStart(2, '0');
    const seconds = String(date.getSeconds()).padStart(2, '0');
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

function formatLogTime(isoString) {
    if (!isoString) return '--:--:--';
    const date = new Date(isoString);
    // Use 24h format: HH:MM:SS
    const hours = String(date.getHours()).padStart(2, '0');
    const minutes = String(date.getMinutes()).padStart(2, '0');
    const seconds = String(date.getSeconds()).padStart(2, '0');
    return `${hours}:${minutes}:${seconds}`;
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

// ==================== Remote Systems ====================

async function loadSystems() {
    try {
        const systems = await api('/systems');
        state.systems = systems;
        
        renderSystemsList();
        updateSystemFilters();
        updateLocalSystemStats();
        
    } catch (error) {
        console.error('Failed to load systems:', error);
        showToast('Failed to load remote systems', 'error');
    }
}

function updateLocalSystemStats() {
    const localContainers = state.containers.filter(c => c.system_id === 'local' || !c.system_id);
    const localCount = document.getElementById('local-container-count');
    const localRunning = document.getElementById('local-running-count');
    
    if (localCount) {
        localCount.textContent = localContainers.length;
    }
    if (localRunning) {
        localRunning.textContent = localContainers.filter(c => c.status === 'running').length;
    }
}

function renderSystemsList() {
    const container = document.getElementById('systems-list');
    if (!container) return;
    
    if (state.systems.length === 0) {
        container.innerHTML = `
            <div class="empty-state systems-empty">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect>
                    <rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect>
                    <line x1="6" y1="6" x2="6.01" y2="6"></line>
                    <line x1="6" y1="18" x2="6.01" y2="18"></line>
                </svg>
                <p>No remote systems configured</p>
                <span>Click "Add System" to monitor a remote Docker host via SSH</span>
            </div>
        `;
        return;
    }
    
    container.innerHTML = state.systems.map(system => `
        <div class="system-card remote" data-system-id="${system.id}">
            <div class="system-header">
                <div class="system-icon remote">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect>
                        <rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect>
                        <line x1="6" y1="6" x2="6.01" y2="6"></line>
                        <line x1="6" y1="18" x2="6.01" y2="18"></line>
                    </svg>
                </div>
                <div class="system-info">
                    <h3>${escapeHtml(system.name)}</h3>
                    <span class="system-host">${escapeHtml(system.username)}@${escapeHtml(system.hostname)}:${system.port}</span>
                </div>
                <span class="system-status ${system.status}">${system.status}</span>
            </div>
            <div class="system-stats">
                <div class="system-stat">
                    <span class="stat-value">${system.container_count}</span>
                    <span class="stat-label">Containers</span>
                </div>
                <div class="system-stat">
                    <span class="stat-value">${system.last_connected ? formatTime(system.last_connected) : 'Never'}</span>
                    <span class="stat-label">Last Connected</span>
                </div>
            </div>
            ${system.last_error ? `<div class="system-error">${escapeHtml(system.last_error)}</div>` : ''}
            <div class="system-actions">
                <button class="btn btn-sm btn-secondary" onclick="testSystemConnection('${system.id}')">
                    Test Connection
                </button>
                <button class="btn btn-sm btn-secondary" onclick="loadRemoteContainers('${system.id}')">
                    View Containers
                </button>
                <button class="btn btn-sm btn-ghost" onclick="editSystem('${system.id}')">
                    Edit
                </button>
                <button class="btn btn-sm btn-ghost btn-danger" onclick="deleteSystem('${system.id}')">
                    Delete
                </button>
            </div>
        </div>
    `).join('');
}

function updateSystemFilters() {
    // Update system filters in Containers and Issues views
    const containersFilter = document.getElementById('containers-system-filter');
    const issuesFilter = document.getElementById('issues-system-filter');
    
    const options = `
        <option value="all">All Systems</option>
        <option value="local">Local</option>
        ${state.systems.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('')}
    `;
    
    if (containersFilter) containersFilter.innerHTML = options;
    if (issuesFilter) issuesFilter.innerHTML = options;
}

function showAddSystemModal() {
    let modal = document.getElementById('system-modal');
    
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'system-modal';
        modal.className = 'modal-overlay';
        document.body.appendChild(modal);
    }
    
    modal.innerHTML = `
        <div class="system-modal-content">
            <div class="modal-header">
                <h2>Add Remote System</h2>
                <button class="btn btn-ghost btn-close" onclick="closeSystemModal()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="18" y1="6" x2="6" y2="18"></line>
                        <line x1="6" y1="6" x2="18" y2="18"></line>
                    </svg>
                </button>
            </div>
            <form onsubmit="createSystem(event)">
                <div class="form-group">
                    <label for="system-name">Name</label>
                    <input type="text" id="system-name" required placeholder="Production Server">
                </div>
                <div class="form-group">
                    <label for="system-hostname">Hostname / IP</label>
                    <input type="text" id="system-hostname" required placeholder="192.168.1.50">
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label for="system-username">Username</label>
                        <input type="text" id="system-username" required placeholder="gildas">
                    </div>
                    <div class="form-group form-group-small">
                        <label for="system-port">SSH Port</label>
                        <input type="number" id="system-port" value="22" min="1" max="65535">
                    </div>
                </div>
                <div class="form-group">
                    <label for="system-ssh-key">SSH Private Key <span class="optional">(optional)</span></label>
                    <textarea id="system-ssh-key" rows="6" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----"></textarea>
                    <span class="form-hint">Paste your private SSH key here. If not provided, the system will use the default SSH agent keys.</span>
                </div>
                <div class="form-note">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
                    </svg>
                    <span>SSH keys are stored securely and used only for Docker daemon connections.</span>
                </div>
                <div class="modal-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeSystemModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Add System</button>
                </div>
            </form>
        </div>
    `;
    
    modal.style.display = 'flex';
}

function closeSystemModal() {
    const modal = document.getElementById('system-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

async function createSystem(event) {
    event.preventDefault();
    
    const sshKey = document.getElementById('system-ssh-key').value.trim();
    
    const data = {
        name: document.getElementById('system-name').value,
        hostname: document.getElementById('system-hostname').value,
        username: document.getElementById('system-username').value,
        port: parseInt(document.getElementById('system-port').value) || 22,
    };
    
    // Only include ssh_key if provided
    if (sshKey) {
        data.ssh_key = sshKey;
    }
    
    try {
        const system = await api('/systems', {
            method: 'POST',
            body: JSON.stringify(data),
        });
        
        state.systems.push(system);
        renderSystemsList();
        updateSystemFilters();
        closeSystemModal();
        
        showToast(`System "${system.name}" added successfully`, 'success');
        
        // Test connection automatically
        testSystemConnection(system.id);
        
    } catch (error) {
        console.error('Failed to create system:', error);
        showToast('Failed to add system', 'error');
    }
}

async function testSystemConnection(systemId) {
    showToast('Testing connection...', 'info');
    
    try {
        const result = await api(`/systems/${systemId}/test`, {
            method: 'POST',
        });
        
        if (result.success) {
            showToast(result.message, 'success');
        } else {
            showToast(`Connection failed: ${result.error}`, 'error');
        }
        
        // Reload systems to update status
        await loadSystems();
        
    } catch (error) {
        console.error('Failed to test connection:', error);
        showToast('Connection test failed', 'error');
    }
}

async function deleteSystem(systemId) {
    const system = state.systems.find(s => s.id === systemId);
    if (!system) return;
    
    if (!confirm(`Are you sure you want to delete "${system.name}"?`)) {
        return;
    }
    
    try {
        await api(`/systems/${systemId}`, {
            method: 'DELETE',
        });
        
        state.systems = state.systems.filter(s => s.id !== systemId);
        renderSystemsList();
        updateSystemFilters();
        
        showToast(`System "${system.name}" deleted`, 'success');
        
    } catch (error) {
        console.error('Failed to delete system:', error);
        showToast('Failed to delete system', 'error');
    }
}

function editSystem(systemId) {
    const system = state.systems.find(s => s.id === systemId);
    if (!system) return;
    
    let modal = document.getElementById('system-modal');
    
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'system-modal';
        modal.className = 'modal-overlay';
        document.body.appendChild(modal);
    }
    
    const hasExistingKey = system.ssh_key ? true : false;
    const keyPlaceholder = hasExistingKey 
        ? '(SSH key is configured - leave empty to keep current key, or paste a new one to replace)'
        : '-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----';
    
    modal.innerHTML = `
        <div class="system-modal-content">
            <div class="modal-header">
                <h2>Edit System</h2>
                <button class="btn btn-ghost btn-close" onclick="closeSystemModal()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="18" y1="6" x2="6" y2="18"></line>
                        <line x1="6" y1="6" x2="18" y2="18"></line>
                    </svg>
                </button>
            </div>
            <form onsubmit="updateSystem(event, '${systemId}')">
                <div class="form-group">
                    <label for="system-name">Name</label>
                    <input type="text" id="system-name" required value="${escapeHtml(system.name)}">
                </div>
                <div class="form-group">
                    <label for="system-hostname">Hostname / IP</label>
                    <input type="text" id="system-hostname" required value="${escapeHtml(system.hostname)}">
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label for="system-username">Username</label>
                        <input type="text" id="system-username" required value="${escapeHtml(system.username)}">
                    </div>
                    <div class="form-group form-group-small">
                        <label for="system-port">SSH Port</label>
                        <input type="number" id="system-port" value="${system.port}" min="1" max="65535">
                    </div>
                </div>
                <div class="form-group">
                    <label for="system-ssh-key">
                        SSH Private Key <span class="optional">(optional)</span>
                        ${hasExistingKey ? '<span class="key-status configured">✓ Key configured</span>' : ''}
                    </label>
                    <textarea id="system-ssh-key" rows="6" placeholder="${keyPlaceholder}"></textarea>
                    <span class="form-hint">${hasExistingKey ? 'Leave empty to keep current key, or paste a new key to replace it.' : 'Paste your private SSH key here.'}</span>
                </div>
                <div class="modal-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeSystemModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Save Changes</button>
                </div>
            </form>
        </div>
    `;
    
    modal.style.display = 'flex';
}

async function updateSystem(event, systemId) {
    event.preventDefault();
    
    const sshKey = document.getElementById('system-ssh-key').value.trim();
    
    const data = {
        name: document.getElementById('system-name').value,
        hostname: document.getElementById('system-hostname').value,
        username: document.getElementById('system-username').value,
        port: parseInt(document.getElementById('system-port').value) || 22,
    };
    
    // Only include ssh_key if a new one is provided
    if (sshKey) {
        data.ssh_key = sshKey;
    }
    
    try {
        const updated = await api(`/systems/${systemId}`, {
            method: 'PUT',
            body: JSON.stringify(data),
        });
        
        const index = state.systems.findIndex(s => s.id === systemId);
        if (index >= 0) {
            state.systems[index] = updated;
        }
        
        renderSystemsList();
        updateSystemFilters();
        closeSystemModal();
        
        showToast('System updated successfully', 'success');
        
    } catch (error) {
        console.error('Failed to update system:', error);
        showToast('Failed to update system', 'error');
    }
}

async function loadRemoteContainers(systemId) {
    // Switch to containers view with system filter
    switchView('containers');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector('[data-view="containers"]').classList.add('active');
    
    // Set the system filter
    const systemFilter = document.getElementById('containers-system-filter');
    if (systemFilter) {
        systemFilter.value = systemId;
        filterContainersBySystem();
    }
}

async function filterContainersBySystem() {
    const systemFilter = document.getElementById('containers-system-filter');
    if (!systemFilter) return;
    
    state.selectedSystem = systemFilter.value;
    
    // Load containers from the selected system(s)
    await loadAllContainers();
}

async function loadAllContainers() {
    try {
        let allContainers = [];
        
        // Load containers based on selected system filter
        if (state.selectedSystem === 'all') {
            // Load from local system
            const localContainers = await api('/containers?all=true');
            localContainers.forEach(c => {
                c.system_id = c.system_id || 'local';
                c.system_name = c.system_name || 'Local';
            });
            allContainers = [...localContainers];
            
            // Load from all remote systems
            for (const system of state.systems) {
                try {
                    const remoteContainers = await api(`/systems/${system.id}/containers?all=true`);
                    allContainers = allContainers.concat(remoteContainers);
                } catch (e) {
                    console.warn(`Failed to load containers from ${system.name}:`, e);
                }
            }
        } else if (state.selectedSystem === 'local') {
            // Load only from local system
            const localContainers = await api('/containers?all=true');
            localContainers.forEach(c => {
                c.system_id = c.system_id || 'local';
                c.system_name = c.system_name || 'Local';
            });
            allContainers = localContainers;
        } else {
            // Load only from specific remote system
            try {
                const remoteContainers = await api(`/systems/${state.selectedSystem}/containers?all=true`);
                allContainers = remoteContainers;
            } catch (e) {
                console.warn(`Failed to load containers from remote system:`, e);
                showToast('Failed to load containers from remote system', 'error');
            }
        }
        
        state.containers = allContainers;
        renderContainersGrid(allContainers);
        updateContainerSelects(allContainers);
        updateContainersCount();
        updateLocalSystemStats();
        
    } catch (error) {
        console.error('Failed to load containers:', error);
        showToast('Failed to load containers', 'error');
    }
}

function filterIssuesBySystem() {
    const systemFilter = document.getElementById('issues-system-filter');
    if (!systemFilter) return;
    
    const selectedSystem = systemFilter.value;
    
    // Save to localStorage for persistence
    localStorage.setItem('logscrawler_issues_system', selectedSystem);
    
    // Filter displayed issues by system
    const issuesList = document.getElementById('issues-list');
    if (!issuesList) return;
    
    const issueCards = issuesList.querySelectorAll('.issue-card');
    issueCards.forEach(card => {
        const issue = state.issues.find(i => i.id === card.dataset.issueId);
        if (!issue) return;
        
        const issueSystem = issue.system_id || 'local';
        
        if (selectedSystem === 'all' || issueSystem === selectedSystem) {
            card.style.display = '';
        } else {
            card.style.display = 'none';
        }
    });
}
