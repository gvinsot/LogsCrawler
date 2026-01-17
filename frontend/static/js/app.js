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

function initNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const view = item.dataset.view;
            switchView(view);
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
    }
    
    // Load charts
    await Promise.all([
        loadErrorsChart(),
        loadHttpChart(),
        loadCpuChart(),
        loadMemoryChart()
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

async function loadCpuChart() {
    const [cpuData, gpuData] = await Promise.all([
        apiGet('/dashboard/cpu-timeseries?hours=24&interval=15m'),
        apiGet('/dashboard/gpu-timeseries?hours=24&interval=15m')
    ]);
    
    if (!cpuData) return;
    
    const ctx = document.getElementById('cpu-chart').getContext('2d');
    
    if (charts.cpu) charts.cpu.destroy();
    
    const datasets = [
        {
            label: 'CPU %',
            data: cpuData.map(p => p.value),
            borderColor: '#00d4aa',
            backgroundColor: 'rgba(0, 212, 170, 0.1)',
            fill: true,
            tension: 0.3
        }
    ];
    
    // Always add GPU line if we have data points (even if all zeros)
    // This allows users to see GPU is being tracked
    if (gpuData && gpuData.length) {
        // Map GPU data to CPU timestamps for alignment
        const gpuValues = cpuData.map((cpuPoint, idx) => {
            const gpuPoint = gpuData[idx];
            return gpuPoint ? (gpuPoint.value || 0) : 0;
        });
        
        datasets.push({
            label: 'GPU %',
            data: gpuValues,
            borderColor: '#f59e0b',
            backgroundColor: 'rgba(245, 158, 11, 0.1)',
            fill: true,
            tension: 0.3
        });
    }
    
    charts.cpu = new Chart(ctx, {
        type: 'line',
        data: {
            labels: cpuData.map(p => formatTime(p.timestamp)),
            datasets: datasets
        },
        options: getChartOptions(true)
    });
}

async function loadMemoryChart() {
    const data = await apiGet('/dashboard/memory-timeseries?hours=24&interval=15m');
    if (!data) return;
    
    const ctx = document.getElementById('memory-chart').getContext('2d');
    
    if (charts.memory) charts.memory.destroy();
    
    charts.memory = new Chart(ctx, {
        type: 'line',
        data: {
            labels: data.map(p => formatTime(p.timestamp)),
            datasets: [{
                label: 'Memory %',
                data: data.map(p => p.value),
                borderColor: '#8b5cf6',
                backgroundColor: 'rgba(139, 92, 246, 0.1)',
                fill: true,
                tension: 0.3
            }]
        },
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

async function loadContainers() {
    const statusFilter = document.getElementById('status-filter').value;
    let endpoint = '/containers/grouped?refresh=true';
    if (statusFilter) {
        endpoint += `&status=${statusFilter}`;
    }
    
    const grouped = await apiGet(endpoint);
    if (!grouped) return;
    
    const container = document.getElementById('containers-list');
    container.innerHTML = '';
    
    for (const [host, projects] of Object.entries(grouped)) {
        const hostDiv = document.createElement('div');
        hostDiv.className = 'host-group';
        
        const containerCount = Object.values(projects).reduce((sum, containers) => sum + containers.length, 0);
        
        let hostHtml = `
            <div class="host-header" onclick="toggleHostGroup(event, this)">
                <span class="host-name">
                    <svg class="chevron-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="6 9 12 15 18 9"/>
                    </svg>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <rect x="2" y="2" width="20" height="8" rx="2" ry="2"/>
                        <rect x="2" y="14" width="20" height="8" rx="2" ry="2"/>
                    </svg>
                    ${escapeHtml(host)}
                    <span class="group-count">${containerCount} containers</span>
                </span>
            </div>
            <div class="host-content">
        `;
        
        for (const [project, containers] of Object.entries(projects)) {
            const projectName = project === '_standalone' ? 'Standalone Containers' : project;
            hostHtml += `
                <div class="compose-group">
                    <div class="compose-header" onclick="toggleComposeGroup(event, this)">
                        <svg class="chevron-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="6 9 12 15 18 9"/>
                        </svg>
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                        </svg>
                        ${escapeHtml(projectName)}
                        <span class="group-count">${containers.length}</span>
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
                
                hostHtml += `
                    <div class="container-item" onclick="openContainer('${escapeHtml(host)}', '${escapeHtml(c.id)}', ${JSON.stringify(c).replace(/"/g, '&quot;')})">
                        <div class="container-info">
                            <span class="container-status ${c.status}"></span>
                            <div>
                                <div class="container-name">${escapeHtml(c.name)}</div>
                                <div class="container-image">${escapeHtml(c.image)}</div>
                            </div>
                        </div>
                        ${c.status === 'running' ? `
                        <div class="container-stats-mini">
                            <span class="stat-mini" title="CPU Usage">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                                    <rect x="4" y="4" width="16" height="16" rx="2"/>
                                    <rect x="9" y="9" width="6" height="6"/>
                                    <path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 15h3M1 9h3M1 15h3"/>
                                </svg>
                                ${cpuDisplay}
                            </span>
                            <span class="stat-mini" title="Memory Usage">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12">
                                    <path d="M2 20h20M6 16V8a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8"/>
                                </svg>
                                ${memDisplay}
                            </span>
                        </div>
                        ` : ''}
                        <div class="container-actions">
                            <button class="btn btn-sm btn-secondary" onclick="event.stopPropagation(); quickAction('${escapeHtml(host)}', '${escapeHtml(c.id)}', 'restart')">
                                Restart
                            </button>
                        </div>
                    </div>
                `;
            }
            
            hostHtml += `
                        </div>
                    </div>
                </div>
            `;
        }
        
        hostHtml += `</div>`; // Close host-content
        
        hostDiv.innerHTML = hostHtml;
        container.appendChild(hostDiv);
    }
}

function toggleHostGroup(event, headerEl) {
    event?.stopPropagation();
    headerEl.closest('.host-group')?.classList.toggle('collapsed');
}

function toggleComposeGroup(event, headerEl) {
    event.stopPropagation();
    headerEl.closest('.compose-group')?.classList.toggle('collapsed');
}

async function refreshContainers() {
    await loadContainers();
}

function filterContainers() {
    loadContainers();
}

// ============== Container Modal ==============

// Store raw logs for filtering
let currentContainerLogs = [];

function openContainer(host, containerId, containerData) {
    currentContainer = { host, id: containerId, data: containerData };
    currentContainerLogs = [];
    
    document.getElementById('modal-container-name').textContent = containerData.name;
    
    // Update status badge
    const statusBadge = document.getElementById('modal-container-status');
    statusBadge.textContent = containerData.status;
    statusBadge.className = `status-badge ${containerData.status}`;
    
    // Clear filter inputs
    document.getElementById('logs-filter').value = '';
    document.getElementById('logs-errors-only').checked = false;
    
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
}

async function refreshContainerLogs() {
    if (!currentContainer) return;
    
    const tail = document.getElementById('logs-tail').value || 500;
    const logs = await apiGet(`/containers/${currentContainer.host}/${currentContainer.id}/logs?tail=${tail}`);
    
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
    
    const stats = await apiGet(`/containers/${currentContainer.host}/${currentContainer.id}/stats`);
    
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

async function quickAction(host, containerId, action) {
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
