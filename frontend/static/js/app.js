/**
 * LogsCrawler Frontend Application
 * Professional Docker Log Analytics Dashboard
 */

// API Base URL
const API_BASE = '/api';

// State
let currentView = 'dashboard';
let currentContainer = null;
let charts = {};
let logsPage = 0;
let logsPageSize = 100;
let totalLogs = 0;

// ============== Initialization ==============

document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    initModalTabs();
    loadDashboard();
});

// ============== Mobile Menu ==============

function toggleMobileMenu() {
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.querySelector('.sidebar-overlay');
    
    sidebar.classList.toggle('mobile-open');
    overlay.classList.toggle('active');
    
    // Prevent body scroll when menu is open
    document.body.style.overflow = sidebar.classList.contains('mobile-open') ? 'hidden' : '';
}

function closeMobileMenu() {
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.querySelector('.sidebar-overlay');
    
    sidebar.classList.remove('mobile-open');
    overlay.classList.remove('active');
    document.body.style.overflow = '';
}

function initNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const view = item.dataset.view;
            switchView(view);
            // Close mobile menu after navigation
            closeMobileMenu();
        });
    });
}

function switchView(view) {
    currentView = view;
    
    // Update nav
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.view === view);
    });
    
    // Update views
    document.querySelectorAll('.view').forEach(v => {
        v.classList.toggle('active', v.id === `${view}-view`);
    });
    
    // Load view data
    switch (view) {
        case 'dashboard':
            loadDashboard();
            break;
        case 'containers':
            loadContainers();
            break;
        case 'logs':
            // Load last 100 logs by default on first visit
            if (totalLogs === 0) {
                loadDefaultLogs();
            }
            checkAIStatus();
            renderRecentQueries();
            break;
        case 'stacks':
            loadStacks();
            break;
    }
}

// ============== AI Search ==============

let aiAvailable = false;

async function checkAIStatus() {
    try {
        const status = await apiGet('/ai/status');
        aiAvailable = status && status.available;
        
        const indicator = document.getElementById('ai-status-indicator');
        if (indicator) {
            indicator.classList.toggle('available', aiAvailable);
            indicator.classList.toggle('unavailable', !aiAvailable);
            indicator.title = aiAvailable ? 'AI Ready' : 'AI Unavailable';
        }
    } catch (e) {
        aiAvailable = false;
    }
}

// ============== Recent Queries ==============

const RECENT_QUERIES_KEY = 'logscrawler_recent_queries';
const MAX_RECENT_QUERIES = 10;

function getRecentQueries() {
    try {
        const stored = localStorage.getItem(RECENT_QUERIES_KEY);
        return stored ? JSON.parse(stored) : [];
    } catch {
        return [];
    }
}

function saveRecentQuery(question, queryParams) {
    const queries = getRecentQueries();
    
    // Remove duplicates
    const filtered = queries.filter(q => q.question.toLowerCase() !== question.toLowerCase());
    
    // Add new query at the beginning
    filtered.unshift({
        question: question,
        params: queryParams,
        timestamp: Date.now()
    });
    
    // Keep only MAX_RECENT_QUERIES
    const trimmed = filtered.slice(0, MAX_RECENT_QUERIES);
    
    localStorage.setItem(RECENT_QUERIES_KEY, JSON.stringify(trimmed));
    renderRecentQueries();
}

function renderRecentQueries() {
    const queries = getRecentQueries();
    const container = document.getElementById('recent-queries');
    const list = document.getElementById('recent-queries-list');
    
    if (!queries.length) {
        container.style.display = 'none';
        return;
    }
    
    list.innerHTML = queries.map((q, idx) => `
        <span class="recent-query-item" onclick="useRecentQuery(${idx})" title="${escapeHtml(q.question)}">
            ${escapeHtml(q.question.length > 40 ? q.question.substring(0, 40) + '...' : q.question)}
            <span class="delete-query" onclick="event.stopPropagation(); deleteRecentQuery(${idx})">✕</span>
        </span>
    `).join('');
    
    container.style.display = 'block';
}

function showRecentQueries() {
    renderRecentQueries();
}

function hideRecentQueries() {
    // Delay hiding to allow click on items
    setTimeout(() => {
        const container = document.getElementById('recent-queries');
        // Don't hide if mouse is over the container
        if (!container.matches(':hover')) {
            // container.style.display = 'none';
        }
    }, 200);
}

function useRecentQuery(index) {
    const queries = getRecentQueries();
    if (index >= 0 && index < queries.length) {
        const query = queries[index];
        document.getElementById('ai-query').value = query.question;
        
        // If we have saved params, use them directly (skip AI call)
        if (query.params) {
            populateGeneratedQuery(query.params);
            
            // Execute search with saved params
            const paginationParams = { ...query.params, sort_order: query.params.sort_order || 'desc' };
            if (query.params.time_range) {
                const now = new Date();
                let start = new Date(now);
                if (query.params.time_range.endsWith('m')) {
                    start.setMinutes(start.getMinutes() - parseInt(query.params.time_range));
                } else if (query.params.time_range.endsWith('h')) {
                    start.setHours(start.getHours() - parseInt(query.params.time_range));
                } else if (query.params.time_range.endsWith('d')) {
                    start.setDate(start.getDate() - parseInt(query.params.time_range));
                }
                paginationParams.start_time = start.toISOString();
                delete paginationParams.time_range;
            }
            lastSearchParams = paginationParams;
            logsPage = 0;
            executeSearchWithParams(paginationParams);
        } else {
            // No saved params, run AI search
            aiSearchLogs();
        }
    }
}

function deleteRecentQuery(index) {
    const queries = getRecentQueries();
    queries.splice(index, 1);
    localStorage.setItem(RECENT_QUERIES_KEY, JSON.stringify(queries));
    renderRecentQueries();
}

function clearRecentQueries() {
    localStorage.removeItem(RECENT_QUERIES_KEY);
    renderRecentQueries();
}

async function aiSearchLogs() {
    const question = document.getElementById('ai-query').value.trim();
    if (!question) return;
    
    const btn = document.getElementById('ai-search-btn');
    const btnText = btn.querySelector('.btn-text');
    const btnLoading = btn.querySelector('.btn-loading');
    
    // Hide recent queries panel
    document.getElementById('recent-queries').style.display = 'none';
    
    // Show loading
    btnText.style.display = 'none';
    btnLoading.style.display = 'inline';
    btn.disabled = true;
    
    try {
        const result = await apiPost('/logs/ai-search', { question });
        
        if (result) {
            const params = result.query_params;
            
            // Save to recent queries (with params for reuse)
            saveRecentQuery(question, params);
            
            // Populate the generated query fields
            populateGeneratedQuery(params);
            
            // Store params for pagination (convert time_range to start_time)
            const paginationParams = { ...params, sort_order: params.sort_order || 'desc' };
            if (params.time_range) {
                const now = new Date();
                let start = new Date(now);
                if (params.time_range.endsWith('m')) {
                    start.setMinutes(start.getMinutes() - parseInt(params.time_range));
                } else if (params.time_range.endsWith('h')) {
                    start.setHours(start.getHours() - parseInt(params.time_range));
                } else if (params.time_range.endsWith('d')) {
                    start.setDate(start.getDate() - parseInt(params.time_range));
                }
                paginationParams.start_time = start.toISOString();
                delete paginationParams.time_range;
            }
            lastSearchParams = paginationParams;
            logsPage = 0;
            
            // Display results
            const searchResult = result.result;
            totalLogs = searchResult.total;
            displayLogsResults(searchResult.hits);
            updatePagination();
        }
    } catch (e) {
        console.error('AI search failed', e);
        alert('AI search failed. Please try again.');
    } finally {
        btnText.style.display = 'inline';
        btnLoading.style.display = 'none';
        btn.disabled = false;
    }
}

function populateGeneratedQuery(params) {
    // Show the generated query panel
    document.getElementById('ai-generated-query').style.display = 'block';
    
    // Build a clean JSON object for display
    const displayParams = {};
    if (params.query) displayParams.query = params.query;
    if (params.levels && params.levels.length) displayParams.levels = params.levels;
    if (params.time_range) displayParams.time_range = params.time_range;
    if (params.http_status_min) displayParams.http_status_min = params.http_status_min;
    if (params.http_status_max) displayParams.http_status_max = params.http_status_max;
    if (params.hosts && params.hosts.length) displayParams.hosts = params.hosts;
    if (params.containers && params.containers.length) displayParams.containers = params.containers;
    displayParams.sort_order = params.sort_order || 'desc';
    
    // Display as formatted JSON
    document.getElementById('gen-query-json').value = JSON.stringify(displayParams, null, 2);
}

async function executeGeneratedQuery() {
    const jsonText = document.getElementById('gen-query-json').value.trim();
    
    let params;
    try {
        params = JSON.parse(jsonText);
    } catch (e) {
        alert('Invalid JSON format. Please check the query syntax.');
        return;
    }
    
    // Convert time_range to start_time if present
    if (params.time_range) {
        const now = new Date();
        let start = new Date(now);
        const tr = params.time_range;
        
        if (tr.endsWith('m')) {
            start.setMinutes(start.getMinutes() - parseInt(tr));
        } else if (tr.endsWith('h')) {
            start.setHours(start.getHours() - parseInt(tr));
        } else if (tr.endsWith('d')) {
            start.setDate(start.getDate() - parseInt(tr));
        }
        
        params.start_time = start.toISOString();
        delete params.time_range;
    }
    
    // Store for pagination and reset page
    lastSearchParams = params;
    logsPage = 0;
    
    try {
        await executeSearchWithParams(params);
    } catch (e) {
        console.error('Query failed', e);
        alert('Search query failed.');
    }
}

async function loadDefaultLogs() {
    // Load the last 100 logs with no filters
    const params = {
        sort_order: 'desc'
    };
    
    // Store for pagination
    lastSearchParams = params;
    logsPage = 0;
    
    await executeSearchWithParams(params);
    document.getElementById('results-count').textContent = `${formatNumber(totalLogs)} total logs (showing latest)`;
}

async function refreshLogsSearch() {
    // If we have a previous search, re-execute it
    if (lastSearchParams) {
        logsPage = 0; // Reset to first page
        await executeSearchWithParams(lastSearchParams);
    } else {
        // Otherwise, load default logs
        await loadDefaultLogs();
    }
}

async function searchHttpErrors(minStatus, maxStatus) {
    // Switch to logs view
    switchView('logs');
    
    // Calculate time range for last 24 hours
    const now = new Date();
    const startTime = new Date(now);
    startTime.setHours(startTime.getHours() - 24);
    
    // Build search params
    const params = {
        http_status_min: minStatus,
        http_status_max: maxStatus,
        start_time: startTime.toISOString(),
        sort_order: 'desc'
    };
    
    // Store for pagination
    lastSearchParams = params;
    logsPage = 0;
    
    // Execute search
    await executeSearchWithParams(params);
    
    // Update results count message
    const statusRange = minStatus === 400 ? '4xx' : '5xx';
    document.getElementById('results-count').textContent = `${formatNumber(totalLogs)} HTTP ${statusRange} errors (last 24h)`;
}

function displayLogsResults(logs) {
    const tbody = document.getElementById('logs-table-body');
    tbody.innerHTML = logs.map((log, index) => `
        <tr class="${getLogRowClass(log)} log-row" data-log-index="${index}" onclick="toggleLogExpand(this, ${index})">
            <td class="col-time">${formatTime(log.timestamp)}</td>
            <td class="col-source" title="${escapeHtml(log.host)} / ${escapeHtml(log.container_name)}">
                <span class="source-host">${escapeHtml(log.host)}</span>
                <span class="source-container">${escapeHtml(log.container_name)}</span>
            </td>
            <td class="col-level">${log.level ? `<span class="log-level ${log.level.toLowerCase()}">${escapeHtml(log.level)}</span>` : ''}</td>
            <td class="col-message"><div class="message-truncate">${escapeHtml(log.message)}</div></td>
        </tr>
        <tr class="log-expand-row" id="log-expand-${index}" style="display: none;">
            <td colspan="4">
                <div class="log-expand-content">
                    <div class="log-full-message">
                        <pre>${escapeHtml(log.message)}</pre>
                    </div>
                    <div class="log-analysis">
                        <div class="analysis-item similar-count">
                            <span class="analysis-label">📊 Similar logs (24h):</span>
                            <span class="analysis-value loading" id="search-similar-${index}">Loading...</span>
                        </div>
                        <div class="analysis-item ai-assessment">
                            <span class="analysis-label">🤖 AI Assessment:</span>
                            <span class="analysis-value loading" id="search-ai-${index}">Analyzing...</span>
                        </div>
                    </div>
                </div>
            </td>
        </tr>
    `).join('');
    
    // Store logs for later reference
    window.currentLogResults = logs;
    
    document.getElementById('results-count').textContent = `${formatNumber(totalLogs)} results`;
    updatePagination();
}

async function toggleLogExpand(row, index) {
    console.log('toggleLogExpand called', index);
    
    const expandRow = document.getElementById(`log-expand-${index}`);
    if (!expandRow) {
        console.error('Expand row not found for index', index);
        return;
    }
    
    const isExpanded = expandRow.style.display !== 'none';
    
    // Close all other expanded rows
    document.querySelectorAll('.log-expand-row').forEach(r => r.style.display = 'none');
    document.querySelectorAll('.log-row').forEach(r => r.classList.remove('expanded'));
    
    if (!isExpanded) {
        expandRow.style.display = 'table-row';
        row.classList.add('expanded');
        
        // Load analysis if not already loaded - use getElementById for reliability
        const similarEl = document.getElementById(`search-similar-${index}`);
        const aiEl = document.getElementById(`search-ai-${index}`);
        
        console.log('Similar element:', similarEl, 'AI element:', aiEl);
        
        if (similarEl && similarEl.classList.contains('loading')) {
            console.log('Loading similar count for index', index);
            loadSimilarCount(index, similarEl);
        }
        if (aiEl && aiEl.classList.contains('loading')) {
            console.log('Loading AI assessment for index', index);
            loadAIAssessment(index, aiEl);
        }
    }
}

async function loadSimilarCount(index, element) {
    try {
        const log = window.currentLogResults[index];
        const result = await apiPost('/logs/similar-count', {
            message: log.message,
            container_name: log.container_name,
            hours: 24
        });
        
        element.classList.remove('loading');
        if (result && result.count !== undefined) {
            const count = result.count;
            element.textContent = count;
            element.classList.add(count > 100 ? 'high' : count > 10 ? 'medium' : 'low');
        } else {
            element.textContent = 'N/A';
        }
    } catch (e) {
        element.classList.remove('loading');
        element.textContent = 'Error';
    }
}

async function loadAIAssessment(index, element) {
    try {
        const log = window.currentLogResults[index];
        const result = await apiPost('/logs/ai-analyze', {
            message: log.message,
            level: log.level,
            container_name: log.container_name
        });
        
        element.classList.remove('loading');
        if (result && result.assessment) {
            element.innerHTML = `
                <span class="assessment-badge ${result.severity}">${result.severity}</span>
                <span class="assessment-text">${escapeHtml(result.assessment)}</span>
            `;
        } else {
            element.textContent = 'Could not analyze';
        }
    } catch (e) {
        element.classList.remove('loading');
        element.textContent = 'AI unavailable';
        element.classList.add('error');
    }
}

function getLogRowClass(log) {
    const msg = (log.message || '').toLowerCase();
    // Use the log level if explicitly set
    if (log.level === 'ERROR' || log.level === 'FATAL' || log.level === 'CRITICAL') return 'log-row-error';
    if (log.level === 'WARN' || log.level === 'WARNING') return 'log-row-warning';
    // Otherwise detect from message content (excluding URL paths)
    if (isErrorLog(msg)) return 'log-row-error';
    if (isWarningLog(msg)) return 'log-row-warning';
    return '';
}

function initModalTabs() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.dataset.tab;

            // Update buttons
            document.querySelectorAll('.tab-btn').forEach(b => {
                b.classList.toggle('active', b.dataset.tab === tab);
            });

            // Update content
            document.querySelectorAll('.tab-content').forEach(c => {
                c.classList.toggle('active', c.id === `tab-${tab}`);
            });

            // Load env vars on demand when switching to env tab
            if (tab === 'env' && currentContainer && Object.keys(currentContainerEnv).length === 0) {
                refreshContainerEnv();
            }
        });
    });
}

// ============== API Helpers ==============

async function apiGet(endpoint) {
    try {
        const response = await fetch(`${API_BASE}${endpoint}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
    } catch (error) {
        console.error(`API Error (${endpoint}):`, error);
        return null;
    }
}

/**
 * API GET with 404 retry support. If the endpoint returns 404,
 * refreshes the container list and retries once.
 */
async function apiGetWithRetry(endpoint, retryCallback = null) {
    try {
        const response = await fetch(`${API_BASE}${endpoint}`);
        if (response.status === 404 && retryCallback) {
            console.log(`Container not found (404), refreshing and retrying...`);
            await retryCallback();
            // Retry once after refresh
            const retryResponse = await fetch(`${API_BASE}${endpoint}`);
            if (!retryResponse.ok) throw new Error(`HTTP ${retryResponse.status}`);
            return await retryResponse.json();
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
    } catch (error) {
        console.error(`API Error (${endpoint}):`, error);
        return null;
    }
}

async function apiPost(endpoint, data) {
    try {
        const response = await fetch(`${API_BASE}${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
    } catch (error) {
        console.error(`API Error (${endpoint}):`, error);
        return null;
    }
}

async function apiPut(endpoint, data) {
    try {
        const response = await fetch(`${API_BASE}${endpoint}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.detail || `HTTP ${response.status}`);
        }
        return await response.json();
    } catch (error) {
        console.error(`API Error (${endpoint}):`, error);
        throw error;
    }
}

function showNotification(type, message) {
    // Remove any existing notification
    const existing = document.querySelector('.notification');
    if (existing) existing.remove();
    
    const notification = document.createElement('div');
    notification.className = `notification notification-${type}`;
    notification.innerHTML = `
        <span class="notification-message">${escapeHtml(message)}</span>
        <button class="notification-close" onclick="this.parentElement.remove()">&times;</button>
    `;
    
    document.body.appendChild(notification);
    
    // Auto-remove after 5 seconds
    setTimeout(() => notification.remove(), 5000);
}

// ============== Dashboard ==============

async function loadDashboard() {
    // Load stats
    const stats = await apiGet('/dashboard/stats');
    if (stats) {
        document.getElementById('running-containers').textContent = stats.running_containers;
        document.getElementById('total-hosts').textContent = stats.total_hosts;
        document.getElementById('http-4xx').textContent = formatNumber(stats.http_4xx_24h);
        document.getElementById('http-5xx').textContent = formatNumber(stats.http_5xx_24h);
        document.getElementById('avg-cpu').textContent = `${stats.avg_cpu_percent.toFixed(1)}%`;
        document.getElementById('avg-memory').textContent = `${stats.avg_memory_percent.toFixed(1)}%`;
        
        // Show GPU stats if available
        if (stats.avg_gpu_percent != null) {
            document.getElementById('gpu-stat-card').style.display = '';
            document.getElementById('avg-gpu').textContent = `${stats.avg_gpu_percent.toFixed(1)}%`;
        } else {
            document.getElementById('gpu-stat-card').style.display = 'none';
        }

        // Show VRAM stats if available
        if (stats.avg_vram_used_mb != null && stats.avg_vram_total_mb != null) {
            document.getElementById('vram-stat-card').style.display = '';
            document.getElementById('avg-vram').textContent = `${formatMemory(stats.avg_vram_used_mb)} / ${formatMemory(stats.avg_vram_total_mb)}`;
        } else {
            document.getElementById('vram-stat-card').style.display = 'none';
        }
    }
    
    // Load charts (with error handling for each)
    await Promise.all([
        loadErrorsChart().catch(e => console.error('Failed to load errors chart:', e)),
        loadHttpChart().catch(e => console.error('Failed to load http chart:', e)),
        loadCpuChart().catch(e => console.error('Failed to load cpu chart:', e)),
        loadGpuChart().catch(e => console.error('Failed to load gpu chart:', e)),
        loadMemoryChart().catch(e => console.error('Failed to load memory chart:', e)),
        loadVramChart().catch(e => console.error('Failed to load vram chart:', e))
    ]);
}

async function refreshDashboard() {
    await loadDashboard();
}

async function loadErrorsChart() {
    const [errorsData, requestsData] = await Promise.all([
        apiGet('/dashboard/errors-timeseries?hours=24&interval=1h'),
        apiGet('/dashboard/http-requests-timeseries?hours=24&interval=1h')
    ]);
    
    if (!errorsData) return;
    
    const ctx = document.getElementById('errors-chart').getContext('2d');
    
    if (charts.errors) charts.errors.destroy();
    
    const datasets = [
        {
            label: 'Errors',
            data: errorsData.map(p => p.value),
            borderColor: '#ef4444',
            backgroundColor: 'rgba(239, 68, 68, 0.1)',
            fill: true,
            tension: 0.3,
            yAxisID: 'y'
        }
    ];
    
    // Add HTTP requests on secondary axis if available
    if (requestsData && requestsData.length) {
        datasets.push({
            label: 'HTTP Requests',
            data: requestsData.map(p => p.value),
            borderColor: '#3b82f6',
            backgroundColor: 'rgba(59, 130, 246, 0.05)',
            fill: true,
            tension: 0.3,
            yAxisID: 'y1'
        });
    }
    
    charts.errors = new Chart(ctx, {
        type: 'line',
        data: {
            labels: errorsData.map(p => formatTime(p.timestamp)),
            datasets: datasets
        },
        options: getChartOptionsDualAxis('Errors', 'Requests')
    });
}

async function loadHttpChart() {
    const [data4xx, data5xx] = await Promise.all([
        apiGet('/dashboard/http-4xx-timeseries?hours=24&interval=1h'),
        apiGet('/dashboard/http-5xx-timeseries?hours=24&interval=1h')
    ]);
    
    if (!data4xx || !data5xx) return;
    
    const ctx = document.getElementById('http-chart').getContext('2d');
    
    if (charts.http) charts.http.destroy();
    
    charts.http = new Chart(ctx, {
        type: 'line',
        data: {
            labels: data4xx.map(p => formatTime(p.timestamp)),
            datasets: [
                {
                    label: '4xx',
                    data: data4xx.map(p => p.value),
                    borderColor: '#f59e0b',
                    backgroundColor: 'rgba(245, 158, 11, 0.1)',
                    fill: true,
                    tension: 0.3
                },
                {
                    label: '5xx',
                    data: data5xx.map(p => p.value),
                    borderColor: '#ef4444',
                    backgroundColor: 'rgba(239, 68, 68, 0.1)',
                    fill: true,
                    tension: 0.3
                }
            ]
        },
        options: getChartOptions()
    });
}

// Color palette for different hosts
const hostColors = [
    '#00d4aa', // teal
    '#f59e0b', // amber
    '#8b5cf6', // purple
    '#ef4444', // red
    '#3b82f6', // blue
    '#ec4899', // pink
    '#14b8a6', // cyan
    '#84cc16', // lime
];

function getHostColor(index) {
    return hostColors[index % hostColors.length];
}

async function loadCpuChart() {
    const data = await apiGet('/dashboard/cpu-timeseries-by-host?hours=24&interval=15m');
    if (!data || !data.length || !data[0].data) return;
    
    const ctx = document.getElementById('cpu-chart').getContext('2d');
    if (charts.cpu) charts.cpu.destroy();
    
    // Get all unique timestamps from first host (they should be aligned)
    const labels = data[0].data.map(p => formatTime(p.timestamp));
    
    // Create a dataset for each host
    const datasets = data.map((hostData, idx) => ({
        label: hostData.host,
        data: hostData.data.map(p => p.value),
        borderColor: getHostColor(idx),
        backgroundColor: 'transparent',
        fill: false,
        tension: 0.3,
        borderWidth: 2
    }));
    
    charts.cpu = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: getChartOptions(true)
    });
}

async function loadGpuChart() {
    const data = await apiGet('/dashboard/gpu-timeseries-by-host?hours=24&interval=15m');
    if (!data || !data.length || !data[0]?.data) {
        // Show empty chart if no GPU data
        const ctx = document.getElementById('gpu-chart').getContext('2d');
        if (charts.gpu) charts.gpu.destroy();
        charts.gpu = new Chart(ctx, {
            type: 'line',
            data: { labels: [], datasets: [] },
            options: { ...getChartOptions(true), plugins: { ...getChartOptions(true).plugins, title: { display: true, text: 'No GPU data available', color: '#6e7681' } } }
        });
        return;
    }
    
    const ctx = document.getElementById('gpu-chart').getContext('2d');
    if (charts.gpu) charts.gpu.destroy();
    
    const labels = data[0].data.map(p => formatTime(p.timestamp));
    
    const datasets = data.map((hostData, idx) => ({
        label: hostData.host,
        data: hostData.data.map(p => p.value),
        borderColor: getHostColor(idx),
        backgroundColor: 'transparent',
        fill: false,
        tension: 0.3,
        borderWidth: 2
    }));
    
    charts.gpu = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: getChartOptions(true)
    });
}

async function loadMemoryChart() {
    const data = await apiGet('/dashboard/memory-timeseries-by-host?hours=24&interval=15m');
    if (!data || !data.length || !data[0].data) return;
    
    const ctx = document.getElementById('memory-chart').getContext('2d');
    if (charts.memory) charts.memory.destroy();
    
    const labels = data[0].data.map(p => formatTime(p.timestamp));
    
    const datasets = data.map((hostData, idx) => ({
        label: hostData.host,
        data: hostData.data.map(p => p.value),
        borderColor: getHostColor(idx),
        backgroundColor: 'transparent',
        fill: false,
        tension: 0.3,
        borderWidth: 2
    }));
    
    charts.memory = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: getChartOptions(true)
    });
}

async function loadVramChart() {
    const data = await apiGet('/dashboard/vram-timeseries-by-host?hours=24&interval=15m');
    if (!data || !data.length || !data[0]?.data) {
        // Hide chart if no VRAM data
        const ctx = document.getElementById('vram-chart').getContext('2d');
        if (charts.vram) charts.vram.destroy();
        charts.vram = new Chart(ctx, {
            type: 'line',
            data: { labels: [], datasets: [] },
            options: { ...getChartOptions(true), plugins: { ...getChartOptions(true).plugins, title: { display: true, text: 'No VRAM data available', color: '#6e7681' } } }
        });
        return;
    }
    
    const ctx = document.getElementById('vram-chart').getContext('2d');
    if (charts.vram) charts.vram.destroy();
    
    const labels = data[0].data.map(p => formatTime(p.timestamp));
    
    const datasets = data.map((hostData, idx) => ({
        label: hostData.host,
        data: hostData.data.map(p => p.value),
        borderColor: getHostColor(idx),
        backgroundColor: 'transparent',
        fill: false,
        tension: 0.3,
        borderWidth: 2
    }));
    
    charts.vram = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: getChartOptions(true)
    });
}

function getChartOptionsDualAxis(leftLabel = 'Left', rightLabel = 'Right') {
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
            mode: 'index',
            intersect: false,
        },
        plugins: {
            legend: {
                display: true,
                position: 'top',
                labels: {
                    color: '#8b949e',
                    font: { family: 'Outfit' }
                }
            }
        },
        scales: {
            x: {
                grid: { color: '#21262d' },
                ticks: { color: '#6e7681', maxRotation: 0 }
            },
            y: {
                type: 'linear',
                display: true,
                position: 'left',
                grid: { color: '#21262d' },
                ticks: { color: '#ef4444' },
                title: {
                    display: true,
                    text: leftLabel,
                    color: '#ef4444'
                }
            },
            y1: {
                type: 'linear',
                display: true,
                position: 'right',
                grid: { drawOnChartArea: false },
                ticks: { color: '#3b82f6' },
                title: {
                    display: true,
                    text: rightLabel,
                    color: '#3b82f6'
                }
            }
        }
    };
}

function getChartOptions(isPercent = false) {
    return {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: {
                display: true,
                position: 'top',
                labels: {
                    color: '#8b949e',
                    font: { family: 'Outfit' }
                }
            }
        },
        scales: {
            x: {
                grid: { color: '#21262d' },
                ticks: { color: '#6e7681', maxRotation: 0 }
            },
            y: {
                grid: { color: '#21262d' },
                ticks: { 
                    color: '#6e7681',
                    callback: isPercent ? (v) => `${v}%` : undefined
                },
                beginAtZero: true,
                max: isPercent ? 100 : undefined
            }
        }
    };
}

// ============== Containers ==============

// Local storage keys
const CONTAINERS_FILTER_KEY = 'logscrawler_containers_filter';
const CONTAINERS_GROUPS_KEY = 'logscrawler_containers_groups';

function getStoredFilter() {
    try {
        return localStorage.getItem(CONTAINERS_FILTER_KEY);
    } catch {
        return null;
    }
}

function saveFilter(value) {
    try {
        localStorage.setItem(CONTAINERS_FILTER_KEY, value);
    } catch (e) {
        console.error('Failed to save filter:', e);
    }
}

function getStoredGroups() {
    try {
        const stored = localStorage.getItem(CONTAINERS_GROUPS_KEY);
        return stored ? JSON.parse(stored) : {};
    } catch {
        return {};
    }
}

function saveGroups(groups) {
    try {
        localStorage.setItem(CONTAINERS_GROUPS_KEY, JSON.stringify(groups));
    } catch (e) {
        console.error('Failed to save groups:', e);
    }
}

function getGroupKey(host, project) {
    return `${host}::${project}`;
}

async function loadContainers(forceRefresh = false) {
    // Restore filters from localStorage
    const storedFilter = getStoredFilter();
    const statusFilter = storedFilter !== null ? storedFilter : document.getElementById('status-filter').value;
    if (storedFilter !== null) {
        document.getElementById('status-filter').value = storedFilter;
    }
    
    // Always use host grouping for Computers view
    const groupBy = 'host';
    
    // forceRefresh ensures backend cache is invalidated
    let endpoint = `/containers/grouped?refresh=true&group_by=${groupBy}`;
    if (statusFilter) {
        endpoint += `&status=${statusFilter}`;
    }
    
    // Fetch containers and host metrics in parallel
    const [grouped, hostMetrics] = await Promise.all([
        apiGet(endpoint),
        apiGet('/hosts/metrics')
    ]);
    if (!grouped) return;
    
    // Get stored group states
    const storedGroups = getStoredGroups();
    
    const container = document.getElementById('containers-list');
    container.innerHTML = '';
    
    const topLevelIcon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="2" y="2" width="20" height="8" rx="2" ry="2"/>
            <rect x="2" y="14" width="20" height="8" rx="2" ry="2"/>
            <line x1="6" y1="6" x2="6.01" y2="6"/>
            <line x1="6" y1="18" x2="6.01" y2="18"/>
        </svg>`;
    
    for (const [topLevel, services] of Object.entries(grouped)) {
        const topLevelDiv = document.createElement('div');
        topLevelDiv.className = 'host-group';
        topLevelDiv.dataset.host = topLevel;
        topLevelDiv.dataset.stack = topLevel;
        
        // Check if top level group should be collapsed
        const topLevelKey = getGroupKey(topLevel, '');
        const isTopLevelCollapsed = storedGroups[topLevelKey] === false;
        if (isTopLevelCollapsed) {
            topLevelDiv.classList.add('collapsed');
        }
        
        const containerCount = Object.values(services).reduce((sum, containers) => sum + containers.length, 0);
        
        // Calculate group stats: total memory and max CPU
        let topLevelTotalMemory = 0;
        let topLevelMaxCpu = 0;
        for (const containers of Object.values(services)) {
            for (const c of containers) {
                if (c.memory_usage_mb != null) topLevelTotalMemory += c.memory_usage_mb;
                if (c.cpu_percent != null && c.cpu_percent > topLevelMaxCpu) topLevelMaxCpu = c.cpu_percent;
            }
        }
        const topLevelMemoryDisplay = topLevelTotalMemory > 0 ? formatMemory(topLevelTotalMemory) : '';
        const topLevelCpuClass = topLevelMaxCpu >= 80 ? 'cpu-critical' : (topLevelMaxCpu >= 50 ? 'cpu-warning' : '');
        const topLevelCpuDisplay = topLevelMaxCpu > 0 ? `${topLevelMaxCpu.toFixed(1)}%` : '';
        
        // Get GPU usage from host metrics
        let topLevelGpuDisplay = '';
        let topLevelGpuClass = '';
        let topLevelVramDisplay = '';
        if (hostMetrics && hostMetrics[topLevel]) {
            const gpuPercent = hostMetrics[topLevel].gpu_percent;
            const gpuMemUsed = hostMetrics[topLevel].gpu_memory_used_mb;
            const gpuMemTotal = hostMetrics[topLevel].gpu_memory_total_mb;
            if (gpuPercent != null) {
                topLevelGpuClass = gpuPercent >= 80 ? 'gpu-critical' : (gpuPercent >= 50 ? 'gpu-warning' : '');
                topLevelGpuDisplay = `${gpuPercent.toFixed(1)}%`;
            }
            if (gpuMemUsed != null && gpuMemTotal != null && gpuMemTotal > 0) {
                const vramPercent = (gpuMemUsed / gpuMemTotal) * 100;
                const vramClass = vramPercent >= 80 ? 'gpu-critical' : (vramPercent >= 50 ? 'gpu-warning' : '');
                topLevelVramDisplay = `<span class="group-stat group-gpu ${vramClass}" title="VRAM usage">🖼️ ${formatMemory(gpuMemUsed)} / ${formatMemory(gpuMemTotal)}</span>`;
            }
        }
        
        let topLevelHtml = `
            <div class="host-header" onclick="toggleHostGroup(event, this)">
                <span class="host-name">
                    <svg class="chevron-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="6 9 12 15 18 9"/>
                    </svg>
                    ${topLevelIcon}
                    ${escapeHtml(topLevel)}
                    <span class="group-count">${containerCount} containers</span>
                    ${topLevelMemoryDisplay ? `<span class="group-stat group-memory" title="RAM - Total memory usage">💾 ${topLevelMemoryDisplay}</span>` : ''}
                    ${topLevelCpuDisplay ? `<span class="group-stat group-cpu ${topLevelCpuClass}" title="CPU - Max usage">⚡ ${topLevelCpuDisplay}</span>` : ''}
                    ${topLevelGpuDisplay ? `<span class="group-stat group-gpu ${topLevelGpuClass}" title="GPU - Compute usage">🎮 ${topLevelGpuDisplay}</span>` : ''}
                    ${topLevelVramDisplay}
                </span>
                <div class="host-header-actions" onclick="event.stopPropagation();">
                    <button class="btn btn-sm btn-warning" onclick="hostAction('${escapeHtml(topLevel)}', 'reboot')" title="Reboot this computer">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                            <polyline points="23 4 23 10 17 10"/>
                            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
                        </svg>
                        <span>Reboot</span>
                    </button>
                    <button class="btn btn-sm btn-danger" onclick="hostAction('${escapeHtml(topLevel)}', 'shutdown')" title="Shutdown this computer">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                            <path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>
                            <line x1="12" y1="2" x2="12" y2="12"/>
                        </svg>
                        <span>Shutdown</span>
                    </button>
                </div>
            </div>
            <div class="host-content">
        `;
        
        for (const [service, containers] of Object.entries(services)) {
            const serviceName = service === '_standalone' ? 'Standalone Containers' : service;
            const groupKey = getGroupKey(topLevel, service);
            const isServiceCollapsed = storedGroups[groupKey] === false;
            const serviceGroupClass = isServiceCollapsed ? 'compose-group collapsed' : 'compose-group';
            
            // Calculate service stats: total memory and max CPU
            let serviceTotalMemory = 0;
            let serviceMaxCpu = 0;
            for (const c of containers) {
                if (c.memory_usage_mb != null) serviceTotalMemory += c.memory_usage_mb;
                if (c.cpu_percent != null && c.cpu_percent > serviceMaxCpu) serviceMaxCpu = c.cpu_percent;
            }
            const serviceMemoryDisplay = serviceTotalMemory > 0 ? formatMemory(serviceTotalMemory) : '';
            const serviceCpuClass = serviceMaxCpu >= 80 ? 'cpu-critical' : (serviceMaxCpu >= 50 ? 'cpu-warning' : '');
            const serviceCpuDisplay = serviceMaxCpu > 0 ? `${serviceMaxCpu.toFixed(1)}%` : '';
            
            // Calculate service GPU stats from host metrics
            let serviceGpuDisplay = '';
            let serviceGpuClass = '';
            let serviceVramDisplay = '';
            if (hostMetrics) {
                // Collect unique hosts for this service's containers
                const serviceHosts = new Set();
                for (const c of containers) {
                    if (c.host) serviceHosts.add(c.host);
                }
                
                // Aggregate GPU metrics: max GPU%, sum VRAM
                let maxGpuPercent = null;
                let totalVramUsed = 0;
                let totalVramTotal = 0;
                let hasVramData = false;
                
                for (const host of serviceHosts) {
                    if (hostMetrics[host]) {
                        const gpuPercent = hostMetrics[host].gpu_percent;
                        const gpuMemUsed = hostMetrics[host].gpu_memory_used_mb;
                        const gpuMemTotal = hostMetrics[host].gpu_memory_total_mb;
                        
                        if (gpuPercent != null) {
                            maxGpuPercent = maxGpuPercent != null ? Math.max(maxGpuPercent, gpuPercent) : gpuPercent;
                        }
                        if (gpuMemUsed != null && gpuMemTotal != null) {
                            totalVramUsed += gpuMemUsed;
                            totalVramTotal += gpuMemTotal;
                            hasVramData = true;
                        }
                    }
                }
                
                if (maxGpuPercent != null) {
                    serviceGpuClass = maxGpuPercent >= 80 ? 'gpu-critical' : (maxGpuPercent >= 50 ? 'gpu-warning' : '');
                    serviceGpuDisplay = `${maxGpuPercent.toFixed(1)}%`;
                }
                if (hasVramData && totalVramTotal > 0) {
                    const vramPercent = (totalVramUsed / totalVramTotal) * 100;
                    const vramClass = vramPercent >= 80 ? 'gpu-critical' : (vramPercent >= 50 ? 'gpu-warning' : '');
                    serviceVramDisplay = `<span class="group-stat group-gpu ${vramClass}" title="VRAM usage">🖼️ ${formatMemory(totalVramUsed)} / ${formatMemory(totalVramTotal)}</span>`;
                }
            }
            
            topLevelHtml += `
                <div class="${serviceGroupClass}" data-host="${escapeHtml(topLevel)}" data-project="${escapeHtml(service)}">
                    <div class="compose-header" onclick="toggleComposeGroup(event, this)">
                        <svg class="chevron-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="6 9 12 15 18 9"/>
                        </svg>
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                        </svg>
                        ${escapeHtml(serviceName)}
                        <span class="group-count">${containers.length}</span>
                        ${serviceMemoryDisplay ? `<span class="group-stat group-memory" title="Total memory usage">💾 ${serviceMemoryDisplay}</span>` : ''}
                        ${serviceCpuDisplay ? `<span class="group-stat group-cpu ${serviceCpuClass}" title="Max CPU usage">⚡ ${serviceCpuDisplay}</span>` : ''}
                        ${serviceGpuDisplay ? `<span class="group-stat group-gpu ${serviceGpuClass}" title="GPU - Max compute usage">🎮 ${serviceGpuDisplay}</span>` : ''}
                        ${serviceVramDisplay}
                    </div>
                    <div class="compose-content">
                        <div class="container-list">
            `;
            
            for (const c of containers) {
                // Format stats display
                const hasStats = c.cpu_percent != null || c.memory_percent != null;
                const cpuDisplay = c.cpu_percent != null ? `${c.cpu_percent}%` : '-';
                const memDisplay = c.memory_percent != null 
                    ? `${c.memory_percent}%${c.memory_usage_mb ? ` (${c.memory_usage_mb}MB)` : ''}`
                    : '-';
                
                const containerNameHtml = escapeHtml(c.name);
                
                topLevelHtml += `
                    <div class="container-item" onclick="openContainer('${escapeHtml(c.host)}', '${escapeHtml(c.id)}', ${JSON.stringify(c).replace(/"/g, '&quot;')})">
                        <div class="container-info">
                            <span class="container-status ${c.status}"></span>
                            <div>
                                <div class="container-name">${containerNameHtml}</div>
                                <div class="container-image">${escapeHtml(c.image)}</div>
                            </div>
                        </div>
                        ${c.status === 'running' ? `
                        <div class="container-stats-mini">
                            <span class="stat-mini" title="CPU %">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                                    <rect x="4" y="4" width="16" height="16" rx="2"/>
                                    <rect x="9" y="9" width="6" height="6"/>
                                    <path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 15h3M1 9h3M1 15h3"/>
                                </svg>
                                ${cpuDisplay}
                            </span>
                            <span class="stat-mini" title="RAM">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                                    <path d="M2 20h20M6 16V8a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8"/>
                                </svg>
                                ${memDisplay}
                            </span>
                        </div>
                        ` : ''}
                        <div class="container-actions">
                            <button class="btn btn-sm btn-secondary" onclick="event.stopPropagation(); quickAction('${escapeHtml(c.host)}', '${escapeHtml(c.id)}', 'restart', '${escapeHtml(c.name)}')">
                                Restart
                            </button>
                            <button class="btn btn-sm btn-danger" onclick="event.stopPropagation(); quickAction('${escapeHtml(c.host)}', '${escapeHtml(c.id)}', 'remove', '${escapeHtml(c.name)}')">
                                Remove
                            </button>
                        </div>
                    </div>
                `;
            }
            
            topLevelHtml += `
                        </div>
                    </div>
                </div>
            `;
        }
        
        topLevelHtml += `</div>`; // Close host-content
        
        topLevelDiv.innerHTML = topLevelHtml;
        container.appendChild(topLevelDiv);
    }
}

function toggleHostGroup(event, headerEl) {
    event?.stopPropagation();
    const hostGroup = headerEl.closest('.host-group');
    if (!hostGroup) return;
    
    const host = hostGroup.dataset.host;
    const isCollapsed = hostGroup.classList.toggle('collapsed');
    
    // Save state
    const storedGroups = getStoredGroups();
    const hostKey = getGroupKey(host, '');
    storedGroups[hostKey] = !isCollapsed; // true = expanded, false = collapsed
    saveGroups(storedGroups);
}

function toggleComposeGroup(event, headerEl) {
    event.stopPropagation();
    const composeGroup = headerEl.closest('.compose-group');
    if (!composeGroup) return;
    
    const host = composeGroup.dataset.host;
    const project = composeGroup.dataset.project;
    const isCollapsed = composeGroup.classList.toggle('collapsed');
    
    // Save state
    const storedGroups = getStoredGroups();
    const groupKey = getGroupKey(host, project);
    storedGroups[groupKey] = !isCollapsed; // true = expanded, false = collapsed
    saveGroups(storedGroups);
}

async function refreshContainers() {
    await loadContainers();
}

function filterContainers() {
    // Save filter value
    const filterValue = document.getElementById('status-filter').value;
    saveFilter(filterValue);
    
    loadContainers();
}

// Host action (reboot/shutdown)
async function hostAction(hostName, action) {
    const actionLabel = action === 'reboot' ? 'reboot' : 'shutdown';
    const confirmMessage = `Are you sure you want to ${actionLabel} the computer "${hostName}"?\n\nThis action cannot be undone and will affect all containers on this host.`;
    
    if (!confirm(confirmMessage)) {
        return;
    }
    
    try {
        const result = await apiPost(`/hosts/${encodeURIComponent(hostName)}/action`, {
            action: action
        });
        
        if (result && result.success) {
            showNotification('success', `${actionLabel.charAt(0).toUpperCase() + actionLabel.slice(1)} command sent to ${hostName}`);
        } else {
            showNotification('error', result?.message || `Failed to ${actionLabel} ${hostName}`);
        }
    } catch (error) {
        console.error(`Failed to ${actionLabel} host:`, error);
        showNotification('error', `Failed to ${actionLabel}: ${error.message || 'Unknown error'}`);
    }
}

// ============== Container Modal ==============

// Store raw logs for filtering
let currentContainerLogs = [];
// Store raw env vars for filtering
let currentContainerEnv = {};

function openContainer(host, containerId, containerData) {
    currentContainer = { host, id: containerId, data: containerData };
    currentContainerLogs = [];
    currentContainerEnv = {};

    document.getElementById('modal-container-name').textContent = containerData.name;

    // Update status badge
    const statusBadge = document.getElementById('modal-container-status');
    statusBadge.textContent = containerData.status;
    statusBadge.className = `status-badge ${containerData.status}`;

    // Clear filter inputs
    document.getElementById('logs-filter').value = '';
    document.getElementById('logs-errors-only').checked = false;
    document.getElementById('env-filter').value = '';

    // Clear env viewer
    document.getElementById('container-env').innerHTML = '';

    // Show info
    const infoDiv = document.getElementById('container-info');
    infoDiv.innerHTML = `
        <div class="info-row"><span class="label">ID:</span><span class="value">${escapeHtml(containerData.id)}</span></div>
        <div class="info-row"><span class="label">Image:</span><span class="value">${escapeHtml(containerData.image)}</span></div>
        <div class="info-row"><span class="label">Status:</span><span class="value">${escapeHtml(containerData.status)}</span></div>
        <div class="info-row"><span class="label">Host:</span><span class="value">${escapeHtml(host)}</span></div>
        <div class="info-row"><span class="label">Compose Project:</span><span class="value">${escapeHtml(containerData.compose_project || '-')}</span></div>
        <div class="info-row"><span class="label">Compose Service:</span><span class="value">${escapeHtml(containerData.compose_service || '-')}</span></div>
        <div class="info-row"><span class="label">Created:</span><span class="value">${formatDateTime(containerData.created)}</span></div>
    `;
    
    // Load logs and stats
    refreshContainerLogs();
    refreshContainerStats();
    
    // Show modal
    document.getElementById('container-modal').classList.add('open');
    
    // Switch to logs tab
    document.querySelector('.tab-btn[data-tab="logs"]').click();
}

function closeModal() {
    document.getElementById('container-modal').classList.remove('open');
    currentContainer = null;
    currentContainerLogs = [];
    currentContainerEnv = {};
}

async function refreshContainerLogs() {
    if (!currentContainer) return;
    
    const tail = document.getElementById('logs-tail').value || 500;
    const logs = await apiGetWithRetry(
        `/containers/${currentContainer.host}/${currentContainer.id}/logs?tail=${tail}`,
        () => loadContainers(true)  // Refresh container list on 404
    );
    
    const logViewer = document.getElementById('container-logs');
    currentContainerLogs = logs || [];
    
    renderContainerLogs();
}

function renderContainerLogs() {
    const logViewer = document.getElementById('container-logs');
    const filterText = document.getElementById('logs-filter').value.toLowerCase();
    const errorsOnly = document.getElementById('logs-errors-only').checked;
    
    if (currentContainerLogs.length === 0) {
        logViewer.innerHTML = '<div class="log-line">No logs available</div>';
        return;
    }
    
    const html = currentContainerLogs.map((log, index) => {
        const message = log.message || '';
        const msgLower = message.toLowerCase();
        
        // Determine log level class - prefer explicit level, then detect from message
        let levelClass = 'log-info';
        const level = (log.level || '').toUpperCase();
        
        if (level === 'ERROR' || level === 'FATAL' || level === 'CRITICAL') {
            levelClass = 'log-error';
        } else if (level === 'WARN' || level === 'WARNING') {
            levelClass = 'log-warning';
        } else if (level === 'DEBUG') {
            levelClass = 'log-debug';
        } else if (isErrorLog(msgLower)) {
            levelClass = 'log-error';
        } else if (isWarningLog(msgLower)) {
            levelClass = 'log-warning';
        } else if (msgLower.includes('debug')) {
            levelClass = 'log-debug';
        }
        
        // Check filters
        let hidden = false;
        if (errorsOnly && levelClass !== 'log-error' && levelClass !== 'log-warning') {
            hidden = true;
        }
        if (filterText && !msgLower.includes(filterText)) {
            hidden = true;
        }
        
        // Highlight search term
        let displayMessage = escapeHtml(message);
        if (filterText && !hidden) {
            const regex = new RegExp(`(${escapeRegex(filterText)})`, 'gi');
            displayMessage = displayMessage.replace(regex, '<span class="log-highlight">$1</span>');
        }
        
        const timestamp = formatDateTime(log.timestamp);
        
        return `
            <div class="log-line ${levelClass}${hidden ? ' hidden' : ''}" data-index="${index}" onclick="toggleContainerLogExpand(this, ${index})">
                <span class="log-timestamp">${timestamp}</span>
                <span class="log-message-truncate">${displayMessage}</span>
            </div>
            <div class="container-log-expand" id="container-log-expand-${index}" style="display: none;">
                <div class="log-expand-content">
                    <div class="log-full-message">
                        <pre>${escapeHtml(message)}</pre>
                    </div>
                    <div class="log-analysis">
                        <div class="analysis-item similar-count">
                            <span class="analysis-label">📊 Similar logs (24h):</span>
                            <span class="analysis-value loading" id="container-similar-${index}">Loading...</span>
                        </div>
                        <div class="analysis-item ai-assessment">
                            <span class="analysis-label">🤖 AI Assessment:</span>
                            <span class="analysis-value loading" id="container-ai-${index}">Analyzing...</span>
                        </div>
                    </div>
                </div>
            </div>
        `;
    }).join('');
    
    logViewer.innerHTML = html;
    logViewer.scrollTop = logViewer.scrollHeight;
}

function toggleContainerLogExpand(element, index) {
    console.log('toggleContainerLogExpand called', index);
    
    const expandDiv = document.getElementById(`container-log-expand-${index}`);
    if (!expandDiv) {
        console.error('Expand div not found for index', index);
        return;
    }
    
    const isExpanded = expandDiv.style.display !== 'none';
    
    // Close all other expanded logs
    document.querySelectorAll('.container-log-expand').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.log-line').forEach(el => el.classList.remove('expanded'));
    
    if (!isExpanded) {
        expandDiv.style.display = 'block';
        element.classList.add('expanded');
        
        // Load analysis
        const similarEl = document.getElementById(`container-similar-${index}`);
        const aiEl = document.getElementById(`container-ai-${index}`);
        
        console.log('Similar element:', similarEl, 'AI element:', aiEl);
        
        if (similarEl && similarEl.classList.contains('loading')) {
            console.log('Loading similar count for index', index);
            loadContainerLogSimilar(index, similarEl);
        }
        if (aiEl && aiEl.classList.contains('loading')) {
            console.log('Loading AI assessment for index', index);
            loadContainerLogAI(index, aiEl);
        }
    }
}

async function loadContainerLogSimilar(index, element) {
    try {
        const log = currentContainerLogs[index];
        const result = await apiPost('/logs/similar-count', {
            message: log.message,
            container_name: currentContainer.data.name,
            hours: 24
        });
        
        element.classList.remove('loading');
        if (result && result.count !== undefined) {
            const count = result.count;
            element.textContent = count;
            element.classList.add(count > 100 ? 'high' : count > 10 ? 'medium' : 'low');
        } else {
            element.textContent = 'N/A';
        }
    } catch (e) {
        element.classList.remove('loading');
        element.textContent = 'Error';
    }
}

async function loadContainerLogAI(index, element) {
    try {
        const log = currentContainerLogs[index];
        const result = await apiPost('/logs/ai-analyze', {
            message: log.message,
            level: log.level || '',
            container_name: currentContainer.data.name
        });
        
        element.classList.remove('loading');
        if (result && result.assessment) {
            element.innerHTML = `
                <span class="assessment-badge ${result.severity}">${result.severity}</span>
                <span class="assessment-text">${escapeHtml(result.assessment)}</span>
            `;
        } else {
            element.textContent = 'Could not analyze';
        }
    } catch (e) {
        element.classList.remove('loading');
        element.textContent = 'AI unavailable';
        element.classList.add('error');
    }
}

function isErrorLog(msg) {
    // Remove URLs from the message before checking for error keywords
    // This prevents "/api/errors-timeseries" from being flagged as an error
    const msgWithoutUrls = removeUrlPaths(msg);
    
    return msgWithoutUrls.includes('error') || msgWithoutUrls.includes('fail') || 
           msgWithoutUrls.includes('fatal') || msgWithoutUrls.includes('exception') || 
           msgWithoutUrls.includes('critical') || msgWithoutUrls.includes('panic');
}

function isWarningLog(msg) {
    const msgWithoutUrls = removeUrlPaths(msg);
    return msgWithoutUrls.includes('warn') || msgWithoutUrls.includes('warning');
}

function removeUrlPaths(msg) {
    // Remove URL paths (e.g., /api/errors-timeseries, GET /path/to/error)
    // Keep the rest of the message for error detection
    return msg
        // Remove quoted URLs like "GET /api/errors HTTP/1.1"
        .replace(/"[A-Z]+\s+\/[^"]*"/g, '')
        // Remove unquoted paths like GET /api/errors
        .replace(/(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+\/\S+/gi, '')
        // Remove standalone URL paths /path/to/something
        .replace(/\/[\w\-\/]+(?:\?[^\s]*)?/g, '');
}

function escapeRegex(string) {
    return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function filterContainerLogs() {
    renderContainerLogs();
}

async function refreshContainerStats() {
    if (!currentContainer) return;

    const stats = await apiGetWithRetry(
        `/containers/${currentContainer.host}/${currentContainer.id}/stats`,
        () => loadContainers(true)  // Refresh container list on 404
    );

    if (stats) {
        document.getElementById('stat-cpu').textContent = `${stats.cpu_percent.toFixed(2)}%`;
        document.getElementById('stat-memory').textContent =
            `${stats.memory_usage_mb.toFixed(1)} MB / ${stats.memory_limit_mb.toFixed(1)} MB (${stats.memory_percent.toFixed(1)}%)`;
        document.getElementById('stat-network').textContent =
            `↓ ${formatBytes(stats.network_rx_bytes)} / ↑ ${formatBytes(stats.network_tx_bytes)}`;
        document.getElementById('stat-block').textContent =
            `Read: ${formatBytes(stats.block_read_bytes)} / Write: ${formatBytes(stats.block_write_bytes)}`;
    } else {
        document.getElementById('stat-cpu').textContent = '-';
        document.getElementById('stat-memory').textContent = '-';
        document.getElementById('stat-network').textContent = '-';
        document.getElementById('stat-block').textContent = '-';
    }
}

async function refreshContainerEnv() {
    if (!currentContainer) return;

    const envViewer = document.getElementById('container-env');
    envViewer.innerHTML = '<div class="loading">Loading environment variables...</div>';

    const envData = await apiGetWithRetry(
        `/containers/${currentContainer.host}/${currentContainer.id}/env`,
        () => loadContainers(true)  // Refresh container list on 404
    );

    if (envData && envData.variables) {
        currentContainerEnv = envData.variables;
        renderContainerEnv();
    } else if (envData && envData.error) {
        envViewer.innerHTML = `<div class="error-message">${escapeHtml(envData.error)}</div>`;
        currentContainerEnv = {};
    } else {
        envViewer.innerHTML = '<div class="error-message">Failed to load environment variables</div>';
        currentContainerEnv = {};
    }
}

function renderContainerEnv() {
    const envViewer = document.getElementById('container-env');
    const filter = document.getElementById('env-filter').value.toLowerCase();

    // Sort keys alphabetically
    const sortedKeys = Object.keys(currentContainerEnv).sort();

    if (sortedKeys.length === 0) {
        envViewer.innerHTML = '<div class="empty-message">No environment variables found</div>';
        return;
    }

    let html = '';
    for (const key of sortedKeys) {
        const value = currentContainerEnv[key];
        const matchesFilter = !filter ||
            key.toLowerCase().includes(filter) ||
            value.toLowerCase().includes(filter);

        html += `
            <div class="env-row${matchesFilter ? '' : ' hidden'}">
                <span class="env-key">${escapeHtml(key)}</span>
                <span class="env-value">${escapeHtml(value)}</span>
            </div>
        `;
    }

    envViewer.innerHTML = html;
}

function filterContainerEnv() {
    renderContainerEnv();
}

async function containerAction(action) {
    if (!currentContainer) return;
    
    const result = await apiPost('/containers/action', {
        host: currentContainer.host,
        container_id: currentContainer.id,
        action: action
    });
    
    if (result) {
        alert(result.message);
        if (result.success) {
            setTimeout(() => {
                refreshContainerStats();
                loadContainers();
            }, 1000);
        }
    }
}

async function quickAction(host, containerId, action, containerName = '') {
    // Show confirmation for destructive actions
    let confirmMessage = '';
    const name = containerName || containerId;
    
    if (action === 'restart') {
        confirmMessage = `Are you sure you want to restart container "${name}"?`;
    } else if (action === 'remove') {
        confirmMessage = `Are you sure you want to remove container "${name}"?\n\nThis action cannot be undone. The container will be stopped and deleted.`;
    }
    
    if (confirmMessage && !confirm(confirmMessage)) {
        return;
    }
    
    const result = await apiPost('/containers/action', {
        host: host,
        container_id: containerId,
        action: action
    });
    
    if (result) {
        alert(result.message);
        setTimeout(loadContainers, 1000);
    }
}

async function removeStack(stackName, host) {
    // Show confirmation
    const confirmMessage = `Are you sure you want to remove the entire Docker Swarm stack "${stackName}"?\n\nThis will remove ALL services and containers in this stack. This action cannot be undone.`;
    
    if (!confirm(confirmMessage)) {
        return;
    }
    
    try {
        const url = `/api/stacks/${encodeURIComponent(stackName)}/remove${host ? `?host=${encodeURIComponent(host)}` : ''}`;
        const response = await fetch(`${API_BASE}${url}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: response.statusText }));
            throw new Error(errorData.detail || `HTTP ${response.status}`);
        }
        
        const result = await response.json();
        
        if (result && result.success) {
            alert(result.message);
            setTimeout(loadContainers, 2000); // Wait a bit longer for stack removal
        } else {
            alert(result?.message || result?.detail || 'Failed to remove stack');
        }
    } catch (error) {
        console.error('Failed to remove stack:', error);
        alert(`Failed to remove stack: ${error.message || 'Unknown error'}`);
    }
}

// Remove deployed stack from Stacks view
async function removeDeployedStack(stackName) {
    // Show confirmation
    const confirmMessage = `Are you sure you want to remove the deployed stack "${stackName}"?\n\nThis will remove ALL services and containers in this stack. This action cannot be undone.`;
    
    if (!confirm(confirmMessage)) {
        return;
    }
    
    try {
        const url = `/api/stacks/${encodeURIComponent(stackName)}/remove`;
        const response = await fetch(`${API_BASE}${url}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: response.statusText }));
            throw new Error(errorData.detail || `HTTP ${response.status}`);
        }
        
        const result = await response.json();
        
        if (result && result.success) {
            showNotification('success', result.message);
            setTimeout(refreshStacks, 2000); // Wait a bit longer for stack removal
        } else {
            showNotification('error', result?.message || result?.detail || 'Failed to remove stack');
        }
    } catch (error) {
        console.error('Failed to remove stack:', error);
        showNotification('error', `Failed to remove stack: ${error.message || 'Unknown error'}`);
    }
}

// ============== Logs Search ==============

// Store last search params for pagination
let lastSearchParams = null;

async function executeSearchWithParams(params) {
    const searchQuery = {
        ...params,
        size: logsPageSize,
        from: logsPage * logsPageSize,
    };
    
    const result = await apiPost('/logs/search', searchQuery);
    if (!result) return;
    
    totalLogs = result.total;
    displayLogsResults(result.hits);
    updatePagination();
}

function updatePagination() {
    const totalPages = Math.ceil(totalLogs / logsPageSize);
    document.getElementById('page-info').textContent = `Page ${logsPage + 1} of ${totalPages || 1}`;
    document.getElementById('prev-page').disabled = logsPage === 0;
    document.getElementById('next-page').disabled = logsPage >= totalPages - 1;
}

function prevPage() {
    if (logsPage > 0 && lastSearchParams) {
        logsPage--;
        executeSearchWithParams(lastSearchParams);
    }
}

function nextPage() {
    const totalPages = Math.ceil(totalLogs / logsPageSize);
    if (logsPage < totalPages - 1 && lastSearchParams) {
        logsPage++;
        executeSearchWithParams(lastSearchParams);
    }
}

function exportLogs() {
    // Simple CSV export
    const table = document.querySelector('.logs-table');
    const rows = Array.from(table.querySelectorAll('tr'));
    
    const csv = rows.map(row => {
        const cells = Array.from(row.querySelectorAll('th, td'));
        return cells.map(cell => `"${cell.textContent.replace(/"/g, '""')}"`).join(',');
    }).join('\n');
    
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `logs-export-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
}

// ============== Stacks (GitHub Integration) ==============

let stacksRepos = [];
let stacksDeployedTags = {};

async function loadStacks() {
    // Check GitHub status
    const status = await apiGet('/stacks/status');
    const statusText = document.getElementById('github-status-text');
    const statusEl = document.getElementById('github-status');
    
    if (status && status.configured) {
        statusText.textContent = status.username ? `@${status.username}` : 'Connected';
        statusEl.classList.add('connected');
        statusEl.classList.remove('disconnected');
    } else {
        statusText.textContent = 'Not configured';
        statusEl.classList.add('disconnected');
        statusEl.classList.remove('connected');
        document.getElementById('stacks-list').innerHTML = `
            <div class="stacks-not-configured">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="48" height="48">
                    <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/>
                </svg>
                <h3>GitHub Integration Not Configured</h3>
                <p>Set the following environment variables to enable:</p>
                <code>LOGSCRAWLER_GITHUB__TOKEN</code><br>
                <code>LOGSCRAWLER_GITHUB__USERNAME</code>
            </div>
        `;
        return;
    }
    
    // Load starred repos
    await refreshStacks();
}

async function refreshStacks() {
    const listEl = document.getElementById('stacks-list');
    listEl.innerHTML = '<div class="loading-placeholder">Loading starred repositories...</div>';
    
    // Load repos, deployed tags, and containers in parallel
    const [reposData, tagsData, containersData, hostMetrics] = await Promise.all([
        apiGet('/stacks/repos'),
        apiGet('/stacks/deployed-tags'),
        apiGet('/containers/grouped?refresh=true&group_by=stack'),
        apiGet('/hosts/metrics')
    ]);
    
    if (!reposData || !reposData.repos) {
        listEl.innerHTML = '<div class="error-placeholder">Failed to load repositories</div>';
        return;
    }
    
    stacksRepos = reposData.repos;
    stacksDeployedTags = (tagsData && tagsData.tags) ? tagsData.tags : {};
    stacksContainers = containersData || {};
    stacksHostMetrics = hostMetrics || {};
    
    if (stacksRepos.length === 0) {
        listEl.innerHTML = '<div class="empty-placeholder">No starred repositories found</div>';
        return;
    }
    
    renderStacksList();
}

// Storage for stack containers and metrics
let stacksContainers = {};
let stacksHostMetrics = {};

// Track expanded stacks
const STACKS_EXPANDED_KEY = 'logscrawler_stacks_expanded';

function getExpandedStacks() {
    try {
        const stored = localStorage.getItem(STACKS_EXPANDED_KEY);
        return stored ? JSON.parse(stored) : {};
    } catch {
        return {};
    }
}

function saveExpandedStacks(expanded) {
    try {
        localStorage.setItem(STACKS_EXPANDED_KEY, JSON.stringify(expanded));
    } catch (e) {
        console.error('Failed to save expanded stacks:', e);
    }
}

function toggleStackExpand(stackName) {
    const expanded = getExpandedStacks();
    expanded[stackName] = !expanded[stackName];
    saveExpandedStacks(expanded);
    
    const hostGroupEl = document.querySelector(`[data-repo="${stackName}"].host-group`);
    const contentEl = document.getElementById(`stack-containers-${stackName}`);
    
    if (hostGroupEl) {
        hostGroupEl.classList.toggle('collapsed', !expanded[stackName]);
    }
    if (contentEl) {
        contentEl.style.display = expanded[stackName] ? 'block' : 'none';
    }
}

function renderStacksList() {
    const listEl = document.getElementById('stacks-list');
    const version = document.getElementById('stack-version')?.value || '1.0';
    const expandedStacks = getExpandedStacks();
    
    // Use containers-grouped class for similar styling to Computers view
    listEl.className = 'containers-grouped';
    
    listEl.innerHTML = stacksRepos.map(repo => {
        const deployedTag = stacksDeployedTags[repo.name];
        const isDeployed = !!deployedTag;
        // Docker stack names are lowercase versions of repo names
        const stackName = repo.name.toLowerCase();
        const stackContainers = stacksContainers[stackName] || {};
        const isExpanded = expandedStacks[repo.name] || false;
        
        // Calculate stack-level stats
        let stackTotalMemory = 0;
        let stackMaxCpu = 0;
        let containerCount = 0;
        
        for (const serviceContainers of Object.values(stackContainers)) {
            for (const c of serviceContainers) {
                containerCount++;
                if (c.memory_usage_mb != null) stackTotalMemory += c.memory_usage_mb;
                if (c.cpu_percent != null && c.cpu_percent > stackMaxCpu) stackMaxCpu = c.cpu_percent;
            }
        }
        
        // Calculate GPU stats from host metrics
        let stackGpuDisplay = '';
        let stackGpuClass = '';
        let stackVramDisplay = '';
        
        if (isDeployed && stacksHostMetrics) {
            const hostsInStack = new Set();
            for (const serviceContainers of Object.values(stackContainers)) {
                for (const c of serviceContainers) {
                    if (c.host) hostsInStack.add(c.host);
                }
            }
            
            let maxGpuPercent = null;
            let totalVramUsed = 0;
            let totalVramTotal = 0;
            let hasVramData = false;
            
            for (const host of hostsInStack) {
                if (stacksHostMetrics[host]) {
                    const gpuPercent = stacksHostMetrics[host].gpu_percent;
                    const gpuMemUsed = stacksHostMetrics[host].gpu_memory_used_mb;
                    const gpuMemTotal = stacksHostMetrics[host].gpu_memory_total_mb;
                    
                    if (gpuPercent != null) {
                        maxGpuPercent = maxGpuPercent != null ? Math.max(maxGpuPercent, gpuPercent) : gpuPercent;
                    }
                    if (gpuMemUsed != null && gpuMemTotal != null) {
                        totalVramUsed += gpuMemUsed;
                        totalVramTotal += gpuMemTotal;
                        hasVramData = true;
                    }
                }
            }
            
            if (maxGpuPercent != null) {
                stackGpuClass = maxGpuPercent >= 80 ? 'gpu-critical' : (maxGpuPercent >= 50 ? 'gpu-warning' : '');
                stackGpuDisplay = `<span class="group-stat group-gpu ${stackGpuClass}" title="GPU - Max usage">🎮 ${maxGpuPercent.toFixed(1)}%</span>`;
            }
            if (hasVramData && totalVramTotal > 0) {
                const vramPercent = (totalVramUsed / totalVramTotal) * 100;
                const vramClass = vramPercent >= 80 ? 'gpu-critical' : (vramPercent >= 50 ? 'gpu-warning' : '');
                stackVramDisplay = `<span class="group-stat group-gpu ${vramClass}" title="VRAM usage">🖼️ ${formatMemory(totalVramUsed)} / ${formatMemory(totalVramTotal)}</span>`;
            }
        }
        
        const stackMemoryDisplay = isDeployed && stackTotalMemory > 0 ? formatMemory(stackTotalMemory) : '';
        const stackCpuClass = stackMaxCpu >= 80 ? 'cpu-critical' : (stackMaxCpu >= 50 ? 'cpu-warning' : '');
        const stackCpuDisplay = isDeployed && stackMaxCpu > 0 ? `${stackMaxCpu.toFixed(1)}%` : '';
        
        // Build containers HTML (similar to Computers view compose-group style)
        let containersHtml = '';
        if (isDeployed && Object.keys(stackContainers).length > 0) {
            containersHtml = `<div class="host-content" id="stack-containers-${escapeHtml(repo.name)}" style="display: ${isExpanded ? 'block' : 'none'};">`;
            
            for (const [serviceName, containers] of Object.entries(stackContainers)) {
                const displayServiceName = serviceName === '_standalone' ? 'Standalone' : serviceName;
                
                // Calculate service stats
                let serviceTotalMemory = 0;
                let serviceMaxCpu = 0;
                for (const c of containers) {
                    if (c.memory_usage_mb != null) serviceTotalMemory += c.memory_usage_mb;
                    if (c.cpu_percent != null && c.cpu_percent > serviceMaxCpu) serviceMaxCpu = c.cpu_percent;
                }
                const serviceMemoryDisplay = serviceTotalMemory > 0 ? formatMemory(serviceTotalMemory) : '';
                const serviceCpuClass = serviceMaxCpu >= 80 ? 'cpu-critical' : (serviceMaxCpu >= 50 ? 'cpu-warning' : '');
                const serviceCpuDisplay = serviceMaxCpu > 0 ? `${serviceMaxCpu.toFixed(1)}%` : '';
                
                containersHtml += `
                    <div class="compose-group">
                        <div class="compose-header">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                            </svg>
                            ${escapeHtml(displayServiceName)}
                            <span class="group-count">${containers.length}</span>
                            ${serviceMemoryDisplay ? `<span class="group-stat group-memory" title="Total memory usage">💾 ${serviceMemoryDisplay}</span>` : ''}
                            ${serviceCpuDisplay ? `<span class="group-stat group-cpu ${serviceCpuClass}" title="Max CPU usage">⚡ ${serviceCpuDisplay}</span>` : ''}
                        </div>
                        <div class="compose-content">
                            <div class="container-list">
                `;
                
                for (const c of containers) {
                    const cpuDisplay = c.cpu_percent != null ? `${c.cpu_percent}%` : '-';
                    const memDisplay = c.memory_percent != null 
                        ? `${c.memory_percent}%${c.memory_usage_mb ? ` (${c.memory_usage_mb}MB)` : ''}`
                        : '-';
                    
                    containersHtml += `
                        <div class="container-item" onclick="openContainer('${escapeHtml(c.host)}', '${escapeHtml(c.id)}', ${JSON.stringify(c).replace(/"/g, '&quot;')})">
                            <div class="container-info">
                                <span class="container-status ${c.status}"></span>
                                <div>
                                    <div class="container-name">${escapeHtml(c.name)} <span style="color: var(--text-muted); font-size: 0.85em;">(${escapeHtml(c.host)})</span></div>
                                    <div class="container-image">${escapeHtml(c.image)}</div>
                                </div>
                            </div>
                            ${c.status === 'running' ? `
                            <div class="container-stats-mini">
                                <span class="stat-mini" title="CPU %">
                                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                                        <rect x="4" y="4" width="16" height="16" rx="2"/>
                                        <rect x="9" y="9" width="6" height="6"/>
                                        <path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 15h3M1 9h3M1 15h3"/>
                                    </svg>
                                    ${cpuDisplay}
                                </span>
                                <span class="stat-mini" title="RAM">
                                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                                        <path d="M2 20h20M6 16V8a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8"/>
                                    </svg>
                                    ${memDisplay}
                                </span>
                            </div>
                            ` : ''}
                            <div class="container-actions">
                                <button class="btn btn-sm btn-secondary" onclick="event.stopPropagation(); quickAction('${escapeHtml(c.host)}', '${escapeHtml(c.id)}', 'restart', '${escapeHtml(c.name)}')">
                                    Restart
                                </button>
                            </div>
                        </div>
                    `;
                }
                
                containersHtml += `
                            </div>
                        </div>
                    </div>
                `;
            }
            
            containersHtml += `</div>`;
        }
        
        // Stack icon
        const stackIcon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
        </svg>`;
        
        // Use host-group structure similar to Computers view
        return `
        <div class="host-group ${isExpanded ? '' : 'collapsed'}" data-repo="${escapeHtml(repo.name)}">
            <div class="host-header" ${isDeployed ? `onclick="toggleStackExpand('${escapeHtml(repo.name)}')"` : ''}>
                <span class="host-name">
                    ${isDeployed ? `
                    <svg class="chevron-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="6 9 12 15 18 9"/>
                    </svg>
                    ` : ''}
                    ${stackIcon}
                    ${escapeHtml(repo.name)}
                    ${deployedTag ? `<span class="stack-badge deployed" title="Deployed version">${escapeHtml(deployedTag)}</span>` : '<span class="stack-badge" style="background: var(--bg-tertiary); color: var(--text-muted);">Not deployed</span>'}
                    ${isDeployed ? `<span class="group-count">${containerCount} containers</span>` : ''}
                    ${stackMemoryDisplay ? `<span class="group-stat group-memory" title="RAM - Total memory usage">💾 ${stackMemoryDisplay}</span>` : ''}
                    ${stackCpuDisplay ? `<span class="group-stat group-cpu ${stackCpuClass}" title="CPU - Max usage">⚡ ${stackCpuDisplay}</span>` : ''}
                    ${stackGpuDisplay}
                    ${stackVramDisplay}
                    ${repo.private ? '<span class="stack-badge private">Private</span>' : ''}
                    ${repo.language ? `<span class="stack-badge lang">${escapeHtml(repo.language)}</span>` : ''}
                </span>
                <div class="host-header-actions" onclick="event.stopPropagation();">
                    <a class="btn btn-sm btn-ghost" href="${escapeHtml(repo.html_url)}" target="_blank" rel="noopener" title="Open on GitHub">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                            <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/>
                        </svg>
                        <span>GitHub</span>
                    </a>
                    <button class="btn btn-sm btn-ghost" onclick="editStackEnv('${escapeHtml(repo.name)}')" title="Edit .env file">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                        </svg>
                        <span>Env</span>
                    </button>
                    <button class="btn btn-sm btn-secondary" onclick="buildStack('${escapeHtml(repo.name)}', '${escapeHtml(repo.ssh_url)}')" id="build-${escapeHtml(repo.name)}" title="Build images">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                            <polyline points="22 4 12 14.01 9 11.01"/>
                        </svg>
                        <span>Build</span>
                    </button>
                    <button class="btn btn-sm btn-primary" onclick="deployStack('${escapeHtml(repo.name)}', '${escapeHtml(repo.ssh_url)}')" id="deploy-${escapeHtml(repo.name)}" title="Deploy stack">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                            <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                            <polyline points="7.5 4.21 12 6.81 16.5 4.21"/>
                            <polyline points="7.5 19.79 7.5 14.6 3 12"/>
                            <polyline points="21 12 16.5 14.6 16.5 19.79"/>
                            <polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
                            <line x1="12" y1="22.08" x2="12" y2="12"/>
                        </svg>
                        <span>Deploy</span>
                    </button>
                    ${isDeployed ? `
                    <button class="btn btn-sm btn-danger" onclick="removeDeployedStack('${escapeHtml(repo.name)}')" title="Remove deployed stack">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                            <polyline points="3 6 5 6 21 6"/>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                        </svg>
                        <span>Remove</span>
                    </button>
                    ` : ''}
                </div>
            </div>
            ${containersHtml}
        </div>
    `;
    }).join('');
}

// ============== Build Modal ==============

let currentBuildRepo = null;
let currentBuildSshUrl = null;

async function buildStack(repoName, sshUrl) {
    currentBuildRepo = repoName;
    currentBuildSshUrl = sshUrl;
    
    const modal = document.getElementById('stack-build-modal');
    const title = document.getElementById('stack-build-title');
    const branchSelect = document.getElementById('build-branch-select');
    const versionInput = document.getElementById('build-version-input');
    const commitInput = document.getElementById('build-commit-input');
    
    title.textContent = `Build: ${repoName}`;
    branchSelect.innerHTML = '<option value="">Loading branches...</option>';
    commitInput.value = '';
    versionInput.value = document.getElementById('stack-version')?.value || '1.0';
    
    // Reset to branch mode
    document.querySelector('input[name="build-source"][value="branch"]').checked = true;
    toggleBuildSource('branch');
    
    modal.classList.add('active');
    
    // Extract owner from ssh_url
    const ownerMatch = sshUrl.match(/[:/]([^/]+)\/[^/]+\.git$/);
    if (!ownerMatch) {
        branchSelect.innerHTML = '<option value="">Failed to parse repository URL</option>';
        return;
    }
    const owner = ownerMatch[1];
    
    // Load branches
    try {
        const data = await apiGet(`/stacks/${encodeURIComponent(owner)}/${encodeURIComponent(repoName)}/branches`);
        if (data && data.branches && data.branches.length > 0) {
            branchSelect.innerHTML = data.branches.map(b => 
                `<option value="${escapeHtml(b.name)}" ${b.name === 'main' || b.name === 'master' ? 'selected' : ''}>
                    ${escapeHtml(b.name)}${b.protected ? ' 🔒' : ''}
                </option>`
            ).join('');
        } else {
            branchSelect.innerHTML = '<option value="main">main</option>';
        }
    } catch (e) {
        console.error('Failed to load branches:', e);
        branchSelect.innerHTML = '<option value="main">main (default)</option>';
    }
}

function toggleBuildSource(source) {
    const branchGroup = document.getElementById('build-branch-group');
    const commitGroup = document.getElementById('build-commit-group');
    
    if (source === 'branch') {
        branchGroup.style.display = 'block';
        commitGroup.style.display = 'none';
    } else {
        branchGroup.style.display = 'none';
        commitGroup.style.display = 'block';
    }
}

function closeBuildModal() {
    document.getElementById('stack-build-modal').classList.remove('active');
    currentBuildRepo = null;
    currentBuildSshUrl = null;
}

async function submitBuild() {
    if (!currentBuildRepo || !currentBuildSshUrl) return;
    
    const submitBtn = document.getElementById('stack-build-submit');
    const source = document.querySelector('input[name="build-source"]:checked').value;
    const version = document.getElementById('build-version-input').value || '1.0';
    
    let branch = null;
    let commit = null;
    
    if (source === 'branch') {
        branch = document.getElementById('build-branch-select').value;
    } else {
        commit = document.getElementById('build-commit-input').value.trim();
        if (!commit) {
            showNotification('error', 'Please enter a commit ID');
            return;
        }
        // Basic validation
        if (!/^[a-fA-F0-9]{7,40}$/.test(commit)) {
            showNotification('error', 'Invalid commit ID format. Expected 7-40 hexadecimal characters.');
            return;
        }
    }
    
    // Disable button and show loading
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="btn-loading"></span> Building...';
    
    try {
        let url = `/stacks/build?repo_name=${encodeURIComponent(currentBuildRepo)}&ssh_url=${encodeURIComponent(currentBuildSshUrl)}&version=${encodeURIComponent(version)}`;
        if (branch) {
            url += `&branch=${encodeURIComponent(branch)}`;
        }
        if (commit) {
            url += `&commit=${encodeURIComponent(commit)}`;
        }
        
        const result = await apiPost(url);
        closeBuildModal();
        showStackOutput('Build', currentBuildRepo, result);
    } catch (e) {
        showNotification('error', e.message || 'Build failed');
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = `
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                <polyline points="22 4 12 14.01 9 11.01"/>
            </svg>
            Build
        `;
    }
}

// ============== Deploy Modal ==============

let currentDeployRepo = null;
let currentDeploySshUrl = null;
let selectedDeployTag = null;

async function deployStack(repoName, sshUrl) {
    currentDeployRepo = repoName;
    currentDeploySshUrl = sshUrl;
    selectedDeployTag = null;
    
    const modal = document.getElementById('stack-deploy-modal');
    const title = document.getElementById('stack-deploy-title');
    const tagsList = document.getElementById('deploy-tags-list');
    const tagInput = document.getElementById('deploy-tag-input');
    const selectedDisplay = document.getElementById('deploy-selected-tag');
    
    title.textContent = `Deploy: ${repoName}`;
    tagsList.innerHTML = '<div class="loading-placeholder">Loading tags...</div>';
    tagInput.value = '';
    selectedDisplay.style.display = 'none';
    
    // Reset to select mode
    document.querySelector('input[name="deploy-source"][value="select"]').checked = true;
    toggleDeploySource('select');
    
    modal.classList.add('active');
    
    // Extract owner from ssh_url
    const ownerMatch = sshUrl.match(/[:/]([^/]+)\/[^/]+\.git$/);
    if (!ownerMatch) {
        tagsList.innerHTML = '<div class="error-placeholder">Failed to parse repository URL</div>';
        return;
    }
    const owner = ownerMatch[1];
    
    // Load tags
    try {
        const data = await apiGet(`/stacks/${encodeURIComponent(owner)}/${encodeURIComponent(repoName)}/tags?limit=20`);
        if (data && data.tags && data.tags.length > 0) {
            renderDeployTagsList(data.tags, data.default_branch || 'main');
            
            // Auto-select the first (most recent) tag
            if (data.tags.length > 0) {
                selectDeployTag(data.tags[0].name);
            }
        } else {
            tagsList.innerHTML = `
                <div class="empty-placeholder">
                    <p>No tags found in this repository.</p>
                    <p class="hint">Use the manual input to enter a tag version.</p>
                </div>
            `;
        }
    } catch (e) {
        console.error('Failed to load tags:', e);
        tagsList.innerHTML = `
            <div class="error-placeholder">
                <p>Failed to load tags: ${escapeHtml(e.message || 'Unknown error')}</p>
                <p class="hint">You can still enter a tag manually.</p>
            </div>
        `;
    }
}

function renderDeployTagsList(tags, defaultBranch) {
    const tagsList = document.getElementById('deploy-tags-list');
    
    // Group tags - for now we show them all in one group
    let html = `
        <div class="tags-group">
            <div class="tags-group-header">
                <span class="branch-name">${escapeHtml(defaultBranch)}</span>
                <span class="tag-count">${tags.length} tag${tags.length !== 1 ? 's' : ''}</span>
            </div>
            <div class="tags-group-items">
    `;
    
    for (const tag of tags) {
        html += `
            <div class="tag-item" data-tag="${escapeHtml(tag.name)}" onclick="selectDeployTag('${escapeHtml(tag.name)}')">
                <span class="tag-name">${escapeHtml(tag.name)}</span>
                <span class="tag-sha">${escapeHtml(tag.sha.substring(0, 7))}</span>
            </div>
        `;
    }
    
    html += `
            </div>
        </div>
    `;
    
    tagsList.innerHTML = html;
}

function selectDeployTag(tagName) {
    selectedDeployTag = tagName;
    
    // Update visual selection
    document.querySelectorAll('.tag-item').forEach(el => {
        el.classList.toggle('selected', el.dataset.tag === tagName);
    });
    
    // Show selected tag display
    const selectedDisplay = document.getElementById('deploy-selected-tag');
    const selectedValue = document.getElementById('deploy-selected-tag-value');
    selectedValue.textContent = tagName;
    selectedDisplay.style.display = 'flex';
}

function toggleDeploySource(source) {
    const selectGroup = document.getElementById('deploy-select-group');
    const manualGroup = document.getElementById('deploy-manual-group');
    const selectedDisplay = document.getElementById('deploy-selected-tag');
    
    if (source === 'select') {
        selectGroup.style.display = 'block';
        manualGroup.style.display = 'none';
        // Restore selection display if we have a selected tag
        if (selectedDeployTag) {
            selectedDisplay.style.display = 'flex';
        }
    } else {
        selectGroup.style.display = 'none';
        manualGroup.style.display = 'block';
        selectedDisplay.style.display = 'none';
    }
}

function closeDeployModal() {
    document.getElementById('stack-deploy-modal').classList.remove('active');
    currentDeployRepo = null;
    currentDeploySshUrl = null;
    selectedDeployTag = null;
}

async function submitDeploy() {
    if (!currentDeployRepo || !currentDeploySshUrl) return;
    
    const submitBtn = document.getElementById('stack-deploy-submit');
    const source = document.querySelector('input[name="deploy-source"]:checked').value;
    const version = document.getElementById('stack-version')?.value || '1.0';
    
    let tag = null;
    
    if (source === 'select') {
        tag = selectedDeployTag;
        if (!tag) {
            showNotification('error', 'Please select a tag to deploy');
            return;
        }
    } else {
        tag = document.getElementById('deploy-tag-input').value.trim();
        if (!tag) {
            showNotification('error', 'Please enter a tag to deploy');
            return;
        }
        // Basic validation for tag format
        if (!/^v?\d+(\.\d+){0,2}$/.test(tag)) {
            showNotification('error', 'Invalid tag format. Expected: vX.X.X or X.X.X (e.g., v1.0.5)');
            return;
        }
    }
    
    // Disable button and show loading
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="btn-loading"></span> Deploying...';
    
    try {
        let url = `/stacks/deploy?repo_name=${encodeURIComponent(currentDeployRepo)}&ssh_url=${encodeURIComponent(currentDeploySshUrl)}&version=${encodeURIComponent(version)}`;
        if (tag) {
            url += `&tag=${encodeURIComponent(tag)}`;
        }
        
        const result = await apiPost(url);
        closeDeployModal();
        showStackOutput('Deploy', currentDeployRepo, result);
        
        // Refresh stacks list after successful deploy
        if (result.success) {
            setTimeout(refreshStacks, 2000);
        }
    } catch (e) {
        showNotification('error', e.message || 'Deploy failed');
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = `
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                <polyline points="7.5 4.21 12 6.81 16.5 4.21"/>
                <polyline points="7.5 19.79 7.5 14.6 3 12"/>
                <polyline points="21 12 16.5 14.6 16.5 19.79"/>
                <polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
                <line x1="12" y1="22.08" x2="12" y2="12"/>
            </svg>
            Deploy
        `;
    }
}

function showStackOutput(action, repoName, result) {
    const modal = document.getElementById('stack-output-modal');
    const title = document.getElementById('stack-output-title');
    const status = document.getElementById('stack-output-status');
    const content = document.getElementById('stack-output-content');
    
    title.textContent = `${action}: ${repoName}`;
    
    if (result.success) {
        status.innerHTML = `
            <span class="status-success">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
                    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                    <polyline points="22 4 12 14.01 9 11.01"/>
                </svg>
                Success
            </span>
            ${result.duration_seconds ? `<span class="status-duration">${result.duration_seconds.toFixed(1)}s</span>` : ''}
            ${result.host ? `<span class="status-host">on ${result.host}</span>` : ''}
        `;
    } else {
        status.innerHTML = `
            <span class="status-error">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20">
                    <circle cx="12" cy="12" r="10"/>
                    <line x1="15" y1="9" x2="9" y2="15"/>
                    <line x1="9" y1="9" x2="15" y2="15"/>
                </svg>
                Failed
            </span>
            ${result.duration_seconds ? `<span class="status-duration">${result.duration_seconds.toFixed(1)}s</span>` : ''}
        `;
    }
    
    content.textContent = result.output || 'No output';
    modal.classList.add('active');
}

function closeStackOutputModal() {
    document.getElementById('stack-output-modal').classList.remove('active');
}

// ============== Stack Env Editor ==============

async function editStackEnv(repoName) {
    const modal = document.getElementById('stack-env-modal');
    const title = document.getElementById('stack-env-title');
    const textarea = document.getElementById('stack-env-content');
    const saveBtn = document.getElementById('stack-env-save');
    
    title.textContent = `Edit .env: ${repoName}`;
    textarea.value = 'Loading...';
    textarea.disabled = true;
    saveBtn.dataset.repo = repoName;
    
    modal.classList.add('active');
    
    try {
        const data = await apiGet(`/stacks/${encodeURIComponent(repoName)}/env`);
        if (data === null) {
            textarea.value = '# .env file not found or failed to load\n# You can create it here\n';
        } else {
            textarea.value = data.content || '';
        }
        textarea.disabled = false;
        textarea.focus();
    } catch (e) {
        textarea.value = `# Error loading .env file: ${e.message || 'Unknown error'}\n# You can create it here\n`;
        textarea.disabled = false;
    }
}

async function saveStackEnv() {
    const modal = document.getElementById('stack-env-modal');
    const textarea = document.getElementById('stack-env-content');
    const saveBtn = document.getElementById('stack-env-save');
    const repoName = saveBtn.dataset.repo;
    
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<span class="btn-loading"></span> Saving...';
    
    try {
        const result = await apiPut(`/stacks/${encodeURIComponent(repoName)}/env`, {
            content: textarea.value
        });
        
        if (result.success) {
            showNotification('success', 'File saved successfully');
            closeStackEnvModal();
        } else {
            showNotification('error', result.message || 'Failed to save file');
        }
    } catch (e) {
        showNotification('error', e.message || 'Failed to save file');
    } finally {
        saveBtn.disabled = false;
        saveBtn.innerHTML = `
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
                <polyline points="17 21 17 13 7 13 7 21"/>
                <polyline points="7 3 7 8 15 8"/>
            </svg>
            Save
        `;
    }
}

function closeStackEnvModal() {
    document.getElementById('stack-env-modal').classList.remove('active');
}

function formatRelativeTime(isoString) {
    if (!isoString) return '';
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);
    
    if (diffMins < 1) return 'just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 30) return `${diffDays}d ago`;
    return date.toLocaleDateString();
}

// ============== Utilities ==============

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function formatNumber(num) {
    if (num === undefined || num === null) return '-';
    return num.toLocaleString();
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function formatMemory(mb) {
    if (mb === 0 || mb == null) return '';
    if (mb >= 1024) {
        return (mb / 1024).toFixed(1) + ' GB';
    }
    return Math.round(mb) + ' MB';
}

function formatTime(isoString) {
    if (!isoString) return '-';
    const date = new Date(isoString);
    return date.toLocaleTimeString('en-US', { 
        hour: '2-digit', 
        minute: '2-digit', 
        second: '2-digit',
        hour12: false 
    });
}

function formatDateTime(isoString) {
    if (!isoString) return '-';
    const date = new Date(isoString);
    return date.toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

// Close modal on escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeModal();
    }
});

// Close modal on backdrop click
document.getElementById('container-modal').addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
        closeModal();
    }
});

// Enter key to search with AI
document.getElementById('ai-query').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        aiSearchLogs();
    }
});

// AI search enter key
document.getElementById('ai-query').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        aiSearchLogs();
    }
});
