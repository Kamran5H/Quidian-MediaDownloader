/**
 * ==========================================================================
 * QUIDIAN — Media Downloader
 * Client controller & real-time progress
 * Developed by Kamran Ashraf
 * ==========================================================================
 */

let activeLinkJobId = null;
let activeProgressTimer = null;
const searchCardJobMap = new Map(); // cardIndex -> { jobId, timer }

// =========================================================================
// DESTINATION DIRECTORY & POP-UP MODAL MANAGEMENT
// =========================================================================
let currentDestination = localStorage.getItem("quidian_dest") || "";
let rememberDestination = localStorage.getItem("quidian_remember_dest") === "true";
let presetPaths = {};
let pendingDownloadAction = null;

async function initDestinationSystem() {
    try {
        const res = await fetch("/api/preset_paths");
        if (res.ok) {
            presetPaths = await res.json();
            if (!currentDestination) {
                currentDestination = presetPaths.default || presetPaths.downloads;
                localStorage.setItem("quidian_dest", currentDestination);
            }
            updateDestinationDisplay();
        }
    } catch (e) {
        console.warn("Could not fetch preset paths:", e);
    }
}

function updateDestinationDisplay() {
    const displayEl = document.getElementById("current-dest-display");
    if (displayEl && currentDestination) {
        displayEl.textContent = currentDestination;
        displayEl.title = currentDestination;
    }
    const modalInput = document.getElementById("modal-dest-input");
    if (modalInput) {
        modalInput.value = currentDestination;
    }
    const rememberToggle = document.getElementById("modal-remember-toggle");
    if (rememberToggle) {
        rememberToggle.checked = rememberDestination;
    }
    highlightPresetChip(currentDestination);
}

function highlightPresetChip(path) {
    document.querySelectorAll(".preset-chip").forEach(c => c.classList.remove("active"));
    if (!path) return;
    const lower = path.toLowerCase().replace(/\\/g, "/");
    if (presetPaths.downloads && lower === presetPaths.downloads.toLowerCase().replace(/\\/g, "/")) {
        document.getElementById("chip-downloads")?.classList.add("active");
    } else if (presetPaths.desktop && lower === presetPaths.desktop.toLowerCase().replace(/\\/g, "/")) {
        document.getElementById("chip-desktop")?.classList.add("active");
    } else if (presetPaths.videos && lower === presetPaths.videos.toLowerCase().replace(/\\/g, "/")) {
        document.getElementById("chip-videos")?.classList.add("active");
    } else if (presetPaths.project && lower === presetPaths.project.toLowerCase().replace(/\\/g, "/")) {
        document.getElementById("chip-project")?.classList.add("active");
    }
}

function selectPresetPath(key, btn) {
    if (presetPaths[key]) {
        currentDestination = presetPaths[key];
        localStorage.setItem("quidian_dest", currentDestination);
        updateDestinationDisplay();
    }
}

async function browseFolderFromModal() {
    const browseBtn = document.querySelector(".btn-browse-native");
    if (browseBtn) {
        browseBtn.disabled = true;
        browseBtn.style.opacity = "0.7";
    }
    try {
        const res = await fetch("/api/browse_folder", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ current: currentDestination })
        });
        const data = await res.json();
        if (data.status === "selected" && data.path) {
            currentDestination = data.path;
            localStorage.setItem("quidian_dest", currentDestination);
            updateDestinationDisplay();
        }
    } catch (e) {
        console.error("Browse folder error:", e);
    } finally {
        if (browseBtn) {
            browseBtn.disabled = false;
            browseBtn.style.opacity = "";
        }
    }
}

function openDestinationModal(actionCallback) {
    pendingDownloadAction = actionCallback || null;
    updateDestinationDisplay();
    const modal = document.getElementById("destination-modal");
    if (modal) modal.classList.remove("hidden");
}

function closeDestinationModal() {
    const modal = document.getElementById("destination-modal");
    if (modal) modal.classList.add("hidden");
    pendingDownloadAction = null;
}

function confirmDestinationDownload() {
    const rememberToggle = document.getElementById("modal-remember-toggle");
    rememberDestination = !!(rememberToggle && rememberToggle.checked);
    localStorage.setItem("quidian_remember_dest", rememberDestination ? "true" : "false");
    localStorage.setItem("quidian_dest", currentDestination);
    updateDestinationDisplay();

    const action = pendingDownloadAction;
    closeDestinationModal();
    if (typeof action === "function") {
        action(currentDestination);
    }
}

function requestDownloadWithDestination(actionCallback, forceModal = false) {
    if (!forceModal && rememberDestination && currentDestination) {
        actionCallback(currentDestination);
    } else {
        openDestinationModal(actionCallback);
    }
}

document.addEventListener("DOMContentLoaded", () => {
    checkStudioHealth();
    initDestinationSystem();
    loadLibrary();

    // Keybindings: Enter key on inputs triggers actions
    document.getElementById("video-url")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") downloadByLink();
    });
    document.getElementById("search-query")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") runSearch();
    });
    document.getElementById("sub-input")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") downloadSubtitle();
    });
    document.getElementById("playlist-url")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") inspectPlaylist();
    });
    document.getElementById("social-url")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") scrapeSocialMedia();
    });
    document.getElementById("audio-source")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") ripAudio();
    });
});

// =========================================================================
// 1. WORKSPACE NAVIGATION & TAB SWITCHING
// =========================================================================
function switchModule(moduleId, btnElement) {
    document.querySelectorAll(".module-view").forEach(mod => mod.classList.remove("active"));
    document.querySelectorAll(".nav-item").forEach(btn => btn.classList.remove("active"));

    const target = document.getElementById(moduleId);
    if (target) target.classList.add("active");
    if (btnElement) btnElement.classList.add("active");

    if (moduleId === "library-module") {
        loadLibrary();
    }
}

function switchInnerTab(e, tabId) {
    document.querySelectorAll(".inner-tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".inner-tab-content").forEach(c => c.classList.remove("active"));

    e.currentTarget.classList.add("active");
    const target = document.getElementById(tabId);
    if (target) target.classList.add("active");
}

function setLang(code, btn) {
    document.querySelectorAll(".lang-chip").forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    const langInput = document.getElementById("sub-lang");
    if (langInput) langInput.value = code;
}

async function pasteToInput(inputId) {
    try {
        const text = await navigator.clipboard.readText();
        const input = document.getElementById(inputId);
        if (input) {
            input.value = text.trim();
            input.focus();
            input.style.boxShadow = "0 0 25px rgba(6, 182, 212, 0.7)";
            setTimeout(() => { input.style.boxShadow = ""; }, 400);
        }
    } catch {
        alert("Clipboard read permission denied. Please paste manually using Ctrl+V.");
    }
}

// =========================================================================
// 2. TELEMETRY & STUDIO HEALTH
// =========================================================================
async function checkStudioHealth() {
    try {
        const res = await fetch("/api/health");
        const data = await res.json();

        // FFmpeg Pill & Warning
        const pillFfmpeg = document.getElementById("pill-ffmpeg");
        const ffmpegWarning = document.getElementById("ffmpeg-warning");
        if (pillFfmpeg) {
            if (data.ffmpeg) {
                pillFfmpeg.innerHTML = `<span class="pulse-dot green"></span>FFmpeg Ready`;
                if (ffmpegWarning) ffmpegWarning.classList.add("hidden");
            } else {
                pillFfmpeg.innerHTML = `<span class="pulse-dot amber"></span>FFmpeg Missing`;
                if (ffmpegWarning) ffmpegWarning.classList.remove("hidden");
            }
        }

        // aria2c Pill
        const pillAria = document.getElementById("pill-aria2");
        if (pillAria) {
            if (data.aria2c) {
                pillAria.innerHTML = `<span class="pulse-dot green"></span>aria2c Multi-Turbo`;
            } else {
                pillAria.innerHTML = `<span class="pulse-dot cyan"></span>Native Multi-Part`;
            }
        }

        // Stealth Bypass Pill
        const pillStealth = document.getElementById("pill-stealth");
        if (pillStealth) {
            pillStealth.innerHTML = `<span class="pulse-dot green"></span>Stealth 403 Bypass Active`;
        }
    } catch (err) {
        console.warn("Studio health check error:", err);
    }
}

function openFolder() {
    fetch("/api/open_folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: currentDestination })
    }).catch(console.error);
}

// =========================================================================
// 3. WORKSPACE: UNIVERSAL DOWNLOADER
// =========================================================================
function downloadByLink() {
    const urlInput = document.getElementById("video-url");
    const statusBox = document.getElementById("link-status");
    const url = urlInput ? urlInput.value.trim() : "";

    if (!url) {
        showStatus(statusBox, "Please provide a valid media link.", "error");
        return;
    }

    requestDownloadWithDestination((chosenPath) => {
        executeDownloadByLink(url, chosenPath);
    });
}

async function executeDownloadByLink(url, outputPath) {
    const qualitySelect = document.getElementById("link-quality");
    const turboToggle = document.getElementById("link-turbo-toggle");
    const stealthToggle = document.getElementById("link-stealth-toggle");
    const statusBox = document.getElementById("link-status");
    const btn = document.getElementById("btn-download-video");
    const progressCard = document.getElementById("link-progress");

    const quality = qualitySelect ? qualitySelect.value : "4k";
    const useTurbo = turboToggle ? turboToggle.checked : true;
    const useStealth = stealthToggle ? stealthToggle.checked : true;

    setBtnLoading(btn, true);
    resetProgress();
    if (progressCard) progressCard.classList.remove("hidden");
    showStatus(statusBox, useStealth ? "Connecting to media stream (stealth bypass active)..." : "Connecting to media stream...", "info");

    try {
        const res = await fetch("/api/download_video", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, quality, use_turbo: useTurbo, use_stealth: useStealth, output_path: outputPath })
        });
        const data = await res.json();

        if (res.ok && data.job_id) {
            activeLinkJobId = data.job_id;
            pollJobProgress(data.job_id, (finalStatus, finalData) => {
                setBtnLoading(btn, false);
                if (finalStatus === "done") {
                    showStatus(statusBox, `Downloaded successfully: "${finalData.title || 'Media'}"`, "success");
                    loadLibrary();
                } else if (finalStatus === "error") {
                    if (finalData && finalData.error_type === "drm_protected") {
                        if (progressCard) progressCard.classList.add("hidden");
                        showDrmNotice(statusBox, finalData);
                    } else {
                        showStatus(statusBox, `Download error: ${finalData.error || 'Failed'}`, "error");
                    }
                } else if (finalStatus === "cancelled") {
                    showStatus(statusBox, "Download task was cancelled by user.", "info");
                }
            });
        } else {
            setBtnLoading(btn, false);
            if (progressCard) progressCard.classList.add("hidden");
            if (data && data.error_type === "drm_protected") {
                showDrmNotice(statusBox, data);
            } else {
                showStatus(statusBox, data.error || "Failed to start download.", "error");
            }
        }
    } catch (err) {
        setBtnLoading(btn, false);
        if (progressCard) progressCard.classList.add("hidden");
        showStatus(statusBox, "Connection failed: " + err.message, "error");
    }
}


function cancelLinkJob() {
    if (!activeLinkJobId) return;
    const cancelBtn = document.getElementById("link-cancel");
    if (cancelBtn) cancelBtn.disabled = true;

    fetch(`/api/cancel/${activeLinkJobId}`, { method: "POST" })
        .then(() => {
            if (activeProgressTimer) clearInterval(activeProgressTimer);
            const statusBox = document.getElementById("link-status");
            const btn = document.getElementById("btn-download-video");
            setBtnLoading(btn, false);
            showStatus(statusBox, "Download cancelled by user.", "info");
            document.getElementById("link-progress-title").textContent = "Cancelled";
        })
        .catch(console.error);
}

function pollJobProgress(jobId, onComplete) {
    if (activeProgressTimer) clearInterval(activeProgressTimer);

    const titleEl = document.getElementById("link-progress-title");
    const fillEl = document.getElementById("link-progress-fill");
    const pctEl = document.getElementById("link-progress-percent");
    const speedEl = document.getElementById("stat-speed");
    const etaEl = document.getElementById("stat-eta");
    const phaseEl = document.getElementById("stat-phase");

    activeProgressTimer = setInterval(async () => {
        try {
            const res = await fetch(`/api/progress/${jobId}`);
            const data = await res.json();

            if (data.status === "downloading") {
                const pct = Math.min(100, Math.max(0, data.percent || 0)).toFixed(0);
                if (fillEl) fillEl.style.width = `${pct}%`;
                if (pctEl) pctEl.textContent = `${pct}%`;
                if (titleEl && data.title) titleEl.textContent = data.title;
                if (speedEl) speedEl.textContent = data.speed ? `⚡ ${data.speed}` : "⚡ Accelerating";
                if (etaEl) etaEl.textContent = data.eta ? `⏱ ETA ${formatDuration(data.eta)}` : "⏱ Estimating";
                if (phaseEl) phaseEl.textContent = data.phase ? `📦 ${data.phase}` : "📦 Stream active";
            } else if (data.status === "processing") {
                if (fillEl) fillEl.style.width = "98%";
                if (pctEl) pctEl.textContent = "98%";
                if (phaseEl) phaseEl.textContent = "⚙️ Merging streams / MP3 conversion";
                if (titleEl && data.title) titleEl.textContent = data.title;
            } else if (data.status === "done") {
                clearInterval(activeProgressTimer);
                if (fillEl) fillEl.style.width = "100%";
                if (pctEl) pctEl.textContent = "100%";
                if (phaseEl) phaseEl.textContent = "✅ Complete";
                if (titleEl && data.title) titleEl.textContent = data.title;
                if (onComplete) onComplete("done", data);
            } else if (data.status === "error") {
                clearInterval(activeProgressTimer);
                if (phaseEl) phaseEl.textContent = "❌ Failed";
                if (onComplete) onComplete("error", data);
            } else if (data.status === "cancelled") {
                clearInterval(activeProgressTimer);
                if (phaseEl) phaseEl.textContent = "⏹ Cancelled";
                if (onComplete) onComplete("cancelled", data);
            }
        } catch {
            // Keep polling on transient network hiccup
        }
    }, 600);
}

function resetProgress() {
    const fill = document.getElementById("link-progress-fill");
    const pct = document.getElementById("link-progress-percent");
    const title = document.getElementById("link-progress-title");
    const cancelBtn = document.getElementById("link-cancel");

    if (fill) fill.style.width = "0%";
    if (pct) pct.textContent = "0%";
    if (title) title.textContent = "Connecting to Stream...";
    if (cancelBtn) cancelBtn.disabled = false;
}

function updateSearchStealthBadge() {
    const toggle = document.getElementById("search-stealth-toggle");
    const label = document.getElementById("search-stealth-badge-label");
    const icon = document.getElementById("search-stealth-icon");
    if (!toggle || !label) return;
    if (toggle.checked) {
        label.innerHTML = "Active &bull; TLS Impersonation &amp; Cloudflare Bypass";
        label.style.color = "var(--text-bright)";
        if (icon) icon.textContent = "🛡️";
    } else {
        label.innerHTML = "Disabled &bull; Standard Direct Pipeline Only";
        label.style.color = "var(--text-dim)";
        if (icon) icon.textContent = "⚠️";
    }
}

// =========================================================================
// 4. WORKSPACE: SEARCH MEDIA
// =========================================================================
async function runSearch() {
    const queryInput = document.getElementById("search-query");
    const includeWeb = document.getElementById("search-include-web");
    const stealthToggle = document.getElementById("search-stealth-toggle");
    const btn = document.getElementById("btn-search");
    const resultsGrid = document.getElementById("search-results");
    const statusBox = document.getElementById("search-status");

    const query = queryInput ? queryInput.value.trim() : "";
    if (!query) {
        showStatus(statusBox, "Please enter a search query.", "error");
        return;
    }

    const useStealth = stealthToggle ? stealthToggle.checked : true;

    // Clean up any lingering card timers from previous searches
    for (const [, entry] of searchCardJobMap.entries()) {
        if (entry && entry.timer) clearInterval(entry.timer);
    }
    searchCardJobMap.clear();

    setBtnLoading(btn, true);
    if (resultsGrid) resultsGrid.innerHTML = "";
    showStatus(statusBox, useStealth ? "Searching global multi-source hub (YouTube, Dailymotion, IMDb, Netflix, Prime, Archive, Web)..." : "Searching all sources...", "info");

    try {
        const res = await fetch("/api/search", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                query,
                count: 24,
                include_web: includeWeb ? includeWeb.checked : true,
                use_stealth: useStealth
            })
        });
        const data = await res.json();
        setBtnLoading(btn, false);

        if (res.ok && data.results && data.results.length > 0) {
            statusBox.classList.remove("show");
            renderSearchResults(data.results, data.cleaned_query, data.original_query || query, data.platform_counts || {});
        } else if (res.ok && (data.status === "empty" || !data.error)) {
            showStatus(statusBox, data.message || "No matching media found across platforms. Try different keywords.", "info");
        } else {
            showStatus(statusBox, data.error || "No matching media found.", "error");
        }
    } catch (err) {
        setBtnLoading(btn, false);
        showStatus(statusBox, "Search error: " + err.message, "error");
    }
}

function fmtViews(n) {
    if (!n || isNaN(n)) return "";
    if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B views";
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M views";
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K views";
    return n + " views";
}

async function downloadBestMatch() {
    const grid = document.getElementById("search-results");
    const firstBtn = grid && grid.querySelector(".btn-card-action:not(.stream-action)");
    if (firstBtn) { firstBtn.click(); firstBtn.scrollIntoView({ behavior: "smooth", block: "center" }); }
}

function openStreamingPlatform(encodedUrl, platformName) {
    const url = decodeURIComponent(encodedUrl);
    window.open(url, "_blank");
    const statusBox = document.getElementById("search-status");
    if (statusBox) {
        showStatus(statusBox, `✨ Opened ${platformName} in browser. Note: Streaming services use DRM protection. For an offline video download, click "Download" on any YouTube, Dailymotion, or Archive.org card!`, "info");
    }
}

function filterResultsByPlatform(platform, chipEl) {
    document.querySelectorAll(".platform-filter-chip").forEach(c => c.classList.remove("active"));
    if (chipEl) chipEl.classList.add("active");

    const cards = document.querySelectorAll(".media-result-card");
    cards.forEach(c => {
        if (platform === "all") {
            c.style.display = "";
        } else {
            const cp = (c.getAttribute("data-platform") || "").toLowerCase().replace(/[^a-z0-9]/g, "");
            const target = platform.toLowerCase().replace(/[^a-z0-9]/g, "");
            c.style.display = (cp === target || cp.includes(target) || target.includes(cp)) ? "" : "none";
        }
    });
}

function renderSearchResults(items, cleanedQuery, originalQuery, platformCounts) {
    const grid = document.getElementById("search-results");
    if (!grid) return;
    grid.innerHTML = "";

    // Platform clean notice if user searched "movie from Netflix / Prime"
    if (cleanedQuery && originalQuery && cleanedQuery.trim().toLowerCase() !== originalQuery.trim().toLowerCase()) {
        const cleanBar = document.createElement("div");
        cleanBar.className = "search-cleaning-notice";
        cleanBar.innerHTML = `
            <span>✨ Platform filter detected: searching for <strong>"${escapeHtml(cleanedQuery)}"</strong> across all sources simultaneously.</span>
        `;
        grid.appendChild(cleanBar);
    }

    // Multi-source Interactive Platform Filter Bar
    const counts = platformCounts || {};
    const platformKeys = Object.keys(counts);
    if (platformKeys.length > 1) {
        const filterBar = document.createElement("div");
        filterBar.className = "platform-filter-bar";
        
        let chipsHtml = `
            <span class="platform-filter-label">Sources:</span>
            <button type="button" class="platform-filter-chip active" onclick="filterResultsByPlatform('all', this)">
                All Sources <span class="chip-count">${items.length}</span>
            </button>
        `;
        platformKeys.forEach(p => {
            chipsHtml += `
                <button type="button" class="platform-filter-chip" onclick="filterResultsByPlatform('${escapeHtml(p)}', this)">
                    ${escapeHtml(p)} <span class="chip-count">${counts[p]}</span>
                </button>
            `;
        });
        filterBar.innerHTML = chipsHtml;
        grid.appendChild(filterBar);
    }

    // One-click "best match" bar (auto-downloads top playable result)
    const playableItems = items.filter(it => !it.is_streaming);
    if (playableItems.length > 0) {
        const topPlayable = playableItems[0];
        const bar = document.createElement("div");
        bar.className = "best-match-bar";
        bar.innerHTML = `
            <div class="best-match-info">
                <span class="best-match-star">⚡</span>
                <span>Top Playable Match: <strong>${escapeHtml((topPlayable.title || '').slice(0, 55))}</strong> (${escapeHtml(topPlayable.platform || 'Direct')})</span>
            </div>
            <button class="btn-best-match" onclick="downloadBestMatch()">Download Best Match</button>
        `;
        grid.appendChild(bar);
    }

    items.forEach((item, idx) => {
        const isWeb = item.source === "web";
        const dur = (isWeb || item.is_streaming) ? "" : formatDuration(item.duration);
        const views = fmtViews(item.view_count);
        const badge = item.badge;
        const platform = item.platform || (item.source === "youtube" ? "YouTube" : (item.source === "dailymotion" ? "Dailymotion" : (item.source === "archive" ? "Archive.org" : (item.uploader || "Web"))));
        const platformClass = platform.toLowerCase().replace(/[^a-z0-9]/g, '');
        const isFullMovie = (item.duration && item.duration >= 2400) || item.badge === "Full Movie" || item.badge === "Archive Movie";
        const isFullVideo = !isFullMovie && ((item.duration && item.duration >= 1200) || item.badge === "Full Video");
        const isStreaming = item.is_streaming || ["netflix", "primevideo", "prime", "imdb"].includes(platformClass);

        // Subtitle line (year/views/channel)
        let metaSubtitle = "";
        if (item.year) {
            metaSubtitle = `${item.year}`;
        } else if (views) {
            metaSubtitle = `${views}`;
        } else if (isStreaming) {
            metaSubtitle = `Streaming`;
        }

        const card = document.createElement("div");
        card.className = "media-result-card" + (idx === 0 ? " top-pick" : "");
        card.id = `media-card-${idx}`;
        card.setAttribute("data-platform", platform);

        const thumbInner = item.thumbnail
            ? `<img src="${item.thumbnail}" alt="Thumbnail" loading="lazy" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=\\'media-thumb-fallback\\'>${isStreaming ? 'STREAM' : '▶'}</div>'">`
            : `<div class="media-thumb-fallback">${isStreaming ? 'STREAM' : (isWeb ? 'WEB' : '▶')}</div>`;

        // Action button based on platform
        let actionBtnHtml = "";
        if (isStreaming) {
            const cleanTitle = (item.title || "").replace(/\s*\((Watch on|IMDb|Netflix|Prime).*?\)/gi, "").trim();
            const encTitle = encodeURIComponent(cleanTitle);
            const encImdb = encodeURIComponent(item.imdb_id || "");
            actionBtnHtml = `
                <div class="media-action-group">
                    <button type="button" class="btn-card-action play-action" onclick="playDirectly(decodeURIComponent('${encTitle}'), decodeURIComponent('${encImdb}'), '${platformClass}', '${encodeURIComponent(item.url)}')">▶ Play (No Login)</button>
                    <button type="button" class="btn-card-action" id="btn-result-${idx}" onclick="downloadSearchResult('${encodeURIComponent(item.url)}', ${idx})">⚡ Download</button>
                </div>
            `;
        } else {
            actionBtnHtml = `<button type="button" class="btn-card-action" id="btn-result-${idx}" onclick="downloadSearchResult('${encodeURIComponent(item.url)}', ${idx})">Download</button>`;
        }

        card.innerHTML = `
            <div class="media-thumb">
                ${thumbInner}
                ${dur ? `<span class="media-dur">${dur}</span>` : ''}
                ${idx === 0 ? `<span class="top-pick-flag">TOP&nbsp;PICK</span>` : ''}
            </div>
            <div class="media-meta">
                <div class="media-title" title="${escapeHtml(item.title || '')}">${escapeHtml(item.title || 'Untitled')}</div>
                <div class="media-uploader">
                    ${isFullMovie ? `<span class="full-movie-badge">🎬 Full Movie</span>` : (isFullVideo ? `<span class="full-movie-badge" style="background: rgba(99,102,241,0.2); border-color: rgba(99,102,241,0.4); color: #a5b4fc;">⏱ Full Video</span>` : '')}
                    ${badge && badge !== 'Full Movie' && badge !== 'Full Video' && badge !== 'Archive Movie' ? `<span class="official-badge">${escapeHtml(badge)}</span>` : ''}
                    ${item.stealth_protected ? `<span class="official-badge" style="background: rgba(168, 85, 247, 0.2); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.4);">🛡️ Stealth</span>` : ''}
                    <span class="uploader-name">${escapeHtml(item.uploader || item.channel || 'Official')}</span>
                    ${metaSubtitle ? `<span class="view-count">• ${escapeHtml(metaSubtitle)}</span>` : ''}
                    <span class="platform-source-tag ${platformClass}">• ${escapeHtml(platform)}</span>
                </div>
            </div>
            <div class="media-action">
                ${actionBtnHtml}
            </div>
            <div class="card-progress-slot hidden" id="card-prog-${idx}">
                <div class="progress-track" style="margin-top: 0.5rem;">
                    <div class="progress-bar-fill" id="card-fill-${idx}"></div>
                </div>
                <div class="card-prog-meta">
                    <span class="card-pct" id="card-pct-${idx}">0%</span>
                    <span class="card-speed" id="card-speed-${idx}"></span>
                </div>
            </div>
        `;
        grid.appendChild(card);
    });
}

function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
}

function downloadSearchResult(encodedUrl, idx) {
    requestDownloadWithDestination((chosenPath) => {
        executeDownloadSearchResult(encodedUrl, idx, chosenPath);
    });
}

async function executeDownloadSearchResult(encodedUrl, idx, outputPath) {
    const url = decodeURIComponent(encodedUrl);
    const quality = document.getElementById("search-quality")?.value || "4k";
    const turboToggle = document.getElementById("search-turbo-toggle");
    const stealthToggle = document.getElementById("search-stealth-toggle");
    const useTurbo = turboToggle ? turboToggle.checked : true;
    const useStealth = stealthToggle ? stealthToggle.checked : true;

    const btn = document.getElementById(`btn-result-${idx}`);
    const progSlot = document.getElementById(`card-prog-${idx}`);
    const fill = document.getElementById(`card-fill-${idx}`);
    const pctEl = document.getElementById(`card-pct-${idx}`);
    const speedElC = document.getElementById(`card-speed-${idx}`);

    if (btn) {
        btn.textContent = useStealth ? "Bypassing locks..." : "Connecting...";
        btn.disabled = true;
    }
    if (progSlot) progSlot.classList.remove("hidden");
    if (pctEl) pctEl.textContent = "0%";

    try {
        const res = await fetch("/api/download_video", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                url,
                quality,
                use_turbo: useTurbo,
                use_stealth: useStealth,
                output_path: outputPath
            })
        });
        const data = await res.json();

        if (res.ok && data.job_id) {
            const jobId = data.job_id;
            const cardEl = document.getElementById(`media-card-${idx}`);
            if (cardEl) cardEl.dataset.activeJobId = jobId;

            if (btn) {
                btn.disabled = false;
                btn.textContent = "Cancel";
                btn.classList.add("cancel-mode");
                btn.onclick = () => cancelSearchCardJob(idx, jobId);
            }

            const timer = setInterval(async () => {
                const currentCard = document.getElementById(`media-card-${idx}`);
                if (!currentCard || currentCard.dataset.activeJobId !== jobId) {
                    clearInterval(timer);
                    return;
                }
                try {
                    const pr = await fetch(`/api/progress/${jobId}`);
                    const j = await pr.json();
                    if (j.status === "downloading") {
                        const pct = Math.min(100, Math.max(0, j.percent || 0)).toFixed(0);
                        if (fill) fill.style.width = `${pct}%`;
                        if (pctEl) pctEl.textContent = `${pct}%  ${j.phase ? '· ' + j.phase : ''}`;
                        if (speedElC) speedElC.textContent = j.speed || "";
                    } else if (j.status === "processing") {
                        if (fill) fill.style.width = "98%";
                        if (pctEl) pctEl.textContent = "98% · finalizing";
                        if (speedElC) speedElC.textContent = "";
                    } else if (j.status === "done") {
                        clearInterval(timer);
                        if (fill) fill.style.width = "100%";
                        if (pctEl) pctEl.textContent = "100% · done";
                        if (speedElC) speedElC.textContent = "";
                        if (btn) {
                            btn.textContent = "Downloaded";
                            btn.className = "btn-card-action done";
                            btn.disabled = true;
                            btn.onclick = null;
                        }
                        loadLibrary();
                    } else if (j.status === "cancelled") {
                        clearInterval(timer);
                        if (fill) fill.style.width = "0%";
                        if (pctEl) pctEl.textContent = "Cancelled";
                        if (speedElC) speedElC.textContent = "";
                        if (btn) {
                            btn.textContent = "Download";
                            btn.className = "btn-card-action";
                            btn.disabled = false;
                            btn.onclick = () => downloadSearchResult(encodedUrl, idx);
                        }
                    } else if (j.status === "error") {
                        clearInterval(timer);
                        if (pctEl) pctEl.textContent = "Failed";
                        if (speedElC) speedElC.textContent = "";
                        if (btn) {
                            btn.textContent = "Retry";
                            btn.className = "btn-card-action failed";
                            btn.disabled = false;
                            btn.onclick = () => downloadSearchResult(encodedUrl, idx);
                        }
                    }
                } catch {
                    // Keep polling
                }
            }, 650);

            searchCardJobMap.set(idx, { jobId, timer });
        } else {
            if (btn) {
                btn.textContent = "Error";
                btn.disabled = true;
            }
        }
    } catch {
        if (btn) {
            btn.textContent = "Error";
            btn.disabled = true;
        }
    }
}

function cancelSearchCardJob(idx, jobId) {
    fetch(`/api/cancel/${jobId}`, { method: "POST" })
        .then(() => {
            const entry = searchCardJobMap.get(idx);
            if (entry && entry.timer) clearInterval(entry.timer);
            const btn = document.getElementById(`btn-result-${idx}`);
            if (btn) {
                btn.textContent = "Cancelled";
                btn.className = "btn-card-action";
                btn.disabled = true;
                btn.onclick = null;
            }
        })
        .catch(console.error);
}

// =========================================================================
// 5. WORKSPACE: PLAYLIST & BATCH STUDIO
// =========================================================================
async function inspectPlaylist() {
    const input = document.getElementById("playlist-url");
    const btn = document.getElementById("btn-inspect-playlist");
    const wrap = document.getElementById("playlist-results-wrap");
    const statusBox = document.getElementById("playlist-status");
    const tbody = document.getElementById("playlist-tbody");

    const url = input ? input.value.trim() : "";
    if (!url) {
        showStatus(statusBox, "Please enter a playlist or channel URL.", "error");
        return;
    }

    setBtnLoading(btn, true);
    showStatus(statusBox, "Inspecting playlist entries without downloading...", "info");
    if (wrap) wrap.classList.add("hidden");

    try {
        const res = await fetch("/api/playlist/inspect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url })
        });
        const data = await res.json();
        setBtnLoading(btn, false);

        if (res.ok && data.entries) {
            statusBox.classList.remove("show");
            document.getElementById("pl-title").textContent = data.title || "Playlist";
            document.getElementById("pl-meta").textContent = `${data.total_items} items • ${data.uploader || 'Creator'}`;

            if (tbody) {
                tbody.innerHTML = "";
                data.entries.forEach((item, index) => {
                    const tr = document.createElement("tr");
                    tr.innerHTML = `
                        <td><input type="checkbox" class="pl-item-check" data-url="${escapeHtml(item.url)}" checked></td>
                        <td style="font-family: var(--font-mono); color: var(--text-dim);">${index + 1}</td>
                        <td style="font-weight: 600;">${escapeHtml(item.title || 'Video ' + (index + 1))}</td>
                        <td style="font-family: var(--font-mono); font-size: 0.8rem;">${formatDuration(item.duration)}</td>
                    `;
                    tbody.appendChild(tr);
                });
            }
            if (wrap) wrap.classList.remove("hidden");
        } else {
            showStatus(statusBox, data.error || "Could not inspect playlist.", "error");
        }
    } catch (err) {
        setBtnLoading(btn, false);
        showStatus(statusBox, "Inspection error: " + err.message, "error");
    }
}

function toggleAllPlaylistCheckboxes(masterCheckbox) {
    const checks = document.querySelectorAll(".pl-item-check");
    checks.forEach(c => c.checked = masterCheckbox.checked);
}

function downloadSelectedPlaylistItems() {
    const checks = document.querySelectorAll(".pl-item-check:checked");
    const statusBox = document.getElementById("playlist-status");

    if (checks.length === 0) {
        showStatus(statusBox, "Please select at least one item from the playlist.", "error");
        return;
    }

    requestDownloadWithDestination((chosenPath) => {
        executePlaylistBatchDownload(checks, statusBox, chosenPath);
    });
}

async function executePlaylistBatchDownload(checks, statusBox, outputPath) {
    const urls = Array.from(checks).map(c => c.getAttribute("data-url"));
    showStatus(statusBox, `Enqueuing batch download for ${urls.length} items to ${outputPath}...`, "info");

    try {
        const res = await fetch("/api/playlist/download", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ urls, quality: "4k", output_path: outputPath, use_turbo: true, use_stealth: true })
        });
        const data = await res.json();
        if (res.ok && data.job_ids && data.job_ids.length > 0) {
            const jobIds = data.job_ids;
            showStatus(statusBox, `⏳ Downloading batch: 0 of ${jobIds.length} completed...`, "info");
            
            const pollInterval = setInterval(async () => {
                try {
                    let done = 0;
                    let errors = 0;
                    for (const jid of jobIds) {
                        const pr = await fetch(`/api/progress/${jid}`);
                        const jd = await pr.json();
                        if (jd.status === "done") done++;
                        else if (jd.status === "error" || jd.status === "cancelled") errors++;
                    }
                    if (done + errors >= jobIds.length) {
                        clearInterval(pollInterval);
                        showStatus(statusBox, `✅ Batch complete! ${done} downloaded, ${errors} failed. Saved to ${outputPath}.`, "success");
                        loadLibrary();
                    } else {
                        showStatus(statusBox, `⏳ Downloading batch: ${done} of ${jobIds.length} completed...`, "info");
                    }
                } catch {
                    // Keep polling
                }
            }, 1500);
        } else {
            showStatus(statusBox, data.error || "Batch queue failed.", "error");
        }
    } catch (err) {
        showStatus(statusBox, "Batch error: " + err.message, "error");
    }
}

function pollModuleJob(jobId, statusBox, btn, defaultSuccessMsg, onDone) {
    setBtnLoading(btn, true);
    const timer = setInterval(async () => {
        try {
            const res = await fetch(`/api/progress/${jobId}`);
            if (!res.ok) return;
            const data = await res.json();
            if (data.status === "downloading" || data.status === "processing") {
                const msg = data.message || "Processing media...";
                const pct = (data.percent && data.percent > 0) ? ` (${data.percent.toFixed(0)}%)` : "";
                showStatus(statusBox, `⏳ ${msg}${pct}`, "info");
            } else if (data.status === "done") {
                clearInterval(timer);
                setBtnLoading(btn, false);
                const finalMsg = data.message || defaultSuccessMsg || "Completed successfully!";
                showStatus(statusBox, `✅ ${finalMsg}`, "success");
                loadLibrary();
                if (typeof onDone === "function") onDone(data);
            } else if (data.status === "error") {
                clearInterval(timer);
                setBtnLoading(btn, false);
                showStatus(statusBox, `❌ Error: ${data.error || 'Operation failed.'}`, "error");
            } else if (data.status === "cancelled") {
                clearInterval(timer);
                setBtnLoading(btn, false);
                showStatus(statusBox, "Operation was cancelled by user.", "info");
            }
        } catch {
            // Keep polling
        }
    }, 750);
}

// =========================================================================
// 6. WORKSPACE: SOCIAL MEDIA SCRAPER (gallery-dl)
// =========================================================================
function scrapeSocialMedia() {
    const input = document.getElementById("social-url");
    const statusBox = document.getElementById("social-status");

    const url = input ? input.value.trim() : "";
    if (!url) {
        showStatus(statusBox, "Please enter a social media link (Instagram, TikTok, Reddit, etc.).", "error");
        return;
    }

    requestDownloadWithDestination((chosenPath) => {
        executeSocialScrape(url, chosenPath);
    });
}

async function executeSocialScrape(url, outputPath) {
    const btn = document.getElementById("btn-social-download");
    const statusBox = document.getElementById("social-status");

    setBtnLoading(btn, true);
    showStatus(statusBox, "Scraping media via gallery-dl engine...", "info");

    try {
        const res = await fetch("/api/social/download", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, output_path: outputPath })
        });
        const data = await res.json();

        if (res.ok && data.job_id) {
            pollModuleJob(data.job_id, statusBox, btn, `Social scrape completed successfully! Files saved to ${outputPath}.`);
        } else {
            setBtnLoading(btn, false);
            showStatus(statusBox, data.error || "Social media scrape failed.", "error");
        }
    } catch (err) {
        setBtnLoading(btn, false);
        showStatus(statusBox, "Scraper error: " + err.message, "error");
    }
}

// =========================================================================
// 7. WORKSPACE: SUBTITLE STUDIO
// =========================================================================
function downloadSubtitle() {
    const input = document.getElementById("sub-input");
    const langInput = document.getElementById("sub-lang");
    const statusBox = document.getElementById("subtitle-status");

    const url = input ? input.value.trim() : "";
    const lang = langInput ? langInput.value.trim() || "en" : "en";

    if (!url) {
        showStatus(statusBox, "Please enter a video URL to extract subtitles.", "error");
        return;
    }

    requestDownloadWithDestination((chosenPath) => {
        executeSubtitleDownload(url, lang, chosenPath);
    });
}

async function executeSubtitleDownload(url, lang, outputPath) {
    const btn = document.getElementById("btn-download-subtitle");
    const statusBox = document.getElementById("subtitle-status");

    setBtnLoading(btn, true);
    showStatus(statusBox, `Fetching and converting subtitles (${lang})...`, "info");

    try {
        const res = await fetch("/api/download_subtitle", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ input_str: url, lang, output_path: outputPath })
        });
        const data = await res.json();

        if (res.ok && data.job_id) {
            pollModuleJob(data.job_id, statusBox, btn, `Subtitles extracted cleanly to ${outputPath}!`);
        } else {
            setBtnLoading(btn, false);
            showStatus(statusBox, "❌ " + (data.error || "No matching subtitles found."), "error");
        }
    } catch (err) {
        setBtnLoading(btn, false);
        showStatus(statusBox, "Subtitle error: " + err.message, "error");
    }
}

// =========================================================================
// 8. WORKSPACE: AUDIO STUDIO & RIPPER
// =========================================================================
function ripAudio() {
    const sourceInput = document.getElementById("audio-source");
    const statusBox = document.getElementById("audio-status");
    const source = sourceInput ? sourceInput.value.trim() : "";

    if (!source) {
        showStatus(statusBox, "Please enter an online URL or local media path.", "error");
        return;
    }

    requestDownloadWithDestination((chosenPath) => {
        executeAudioRip(source, chosenPath);
    });
}

async function executeAudioRip(source, outputPath) {
    const formatSelect = document.getElementById("audio-format");
    const bitrateSelect = document.getElementById("audio-bitrate");
    const titleInput = document.getElementById("audio-tag-title");
    const artistInput = document.getElementById("audio-tag-artist");
    const albumInput = document.getElementById("audio-tag-album");
    const btn = document.getElementById("btn-rip-audio");
    const statusBox = document.getElementById("audio-status");

    const targetFmt = formatSelect ? formatSelect.value : "mp3";
    setBtnLoading(btn, true);
    showStatus(statusBox, "Ripping audio and applying ID3 metadata tags...", "info");

    try {
        const res = await fetch("/api/audio/rip", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                source,
                output_path: outputPath,
                format: targetFmt,
                bitrate: bitrateSelect ? bitrateSelect.value : "320k",
                metadata: {
                    title: titleInput ? titleInput.value.trim() : "",
                    artist: artistInput ? artistInput.value.trim() : "",
                    album: albumInput ? albumInput.value.trim() : ""
                }
            })
        });
        const data = await res.json();

        if (res.ok && data.job_id) {
            pollModuleJob(data.job_id, statusBox, btn, `Audio ripped & tagged with ID3 successfully into ${outputPath}!`);
        } else {
            setBtnLoading(btn, false);
            showStatus(statusBox, data.error || "Audio ripping failed.", "error");
        }
    } catch (err) {
        setBtnLoading(btn, false);
        showStatus(statusBox, "Audio error: " + err.message, "error");
    }
}


// =========================================================================
// 9. WORKSPACE: DOWNLOADS LIBRARY
// =========================================================================
async function loadLibrary() {
    const grid = document.getElementById("library-items-grid");
    const pathLabel = document.getElementById("lib-path-label");
    if (!grid) return;

    try {
        const queryDir = currentDestination ? `?dir=${encodeURIComponent(currentDestination)}` : "";
        const res = await fetch(`/api/library${queryDir}`);
        const data = await res.json();

        if (res.ok && data.files) {
            if (pathLabel) pathLabel.textContent = `Location: ${data.directory} (${data.files.length} items)`;
            grid.innerHTML = "";

            if (data.files.length === 0) {
                grid.innerHTML = `<p style="color: var(--text-dim); font-size: 0.9rem; grid-column: 1/-1;">No media files found in your Downloads folder yet.</p>`;
                return;
            }

            data.files.forEach(file => {
                const card = document.createElement("div");
                card.className = "library-card";
                const sizeDisplay = file.size_formatted || (file.size_mb ? `${file.size_mb} MB` : "");
                const dateDisplay = file.modified || file.date || "";
                card.innerHTML = `
                    <div class="library-card-top">
                        <div class="lib-file-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</div>
                        <span class="lib-ext-badge">${escapeHtml(file.ext || file.type || 'MEDIA')}</span>
                    </div>
                    <div class="lib-meta-row">
                        <span>${escapeHtml(sizeDisplay)}</span>
                        <span>${escapeHtml(dateDisplay)}</span>
                    </div>
                    <div style="display: flex; gap: 0.5rem; margin-top: 0.6rem;">
                        <button class="btn-lib-action" style="flex: 1.2;" onclick="openLibraryFile('${encodeURIComponent(file.path)}')">▶ Play / Open</button>
                        <button class="btn-lib-action" style="flex: 0.8; background: rgba(255,255,255,0.07); border-color: rgba(255,255,255,0.15);" onclick="revealLibraryFile('${encodeURIComponent(file.path)}')" title="Highlight in Windows File Explorer">📁 Reveal</button>
                    </div>
                `;
                grid.appendChild(card);
            });
        }
    } catch {
        if (grid) grid.innerHTML = `<p style="color: #f87171;">Could not load library contents.</p>`;
    }
}

function openLibraryFile(encodedPath) {
    const path = decodeURIComponent(encodedPath);
    fetch("/api/library/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_path: path })
    }).catch(console.error);
}

function revealLibraryFile(encodedPath) {
    const path = decodeURIComponent(encodedPath);
    fetch("/api/library/reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_path: path })
    }).catch(console.error);
}

// =========================================================================
// 10. UTILITIES
// =========================================================================
function showStatus(box, msg, type) {
    if (!box) return;
    box.className = `status-box show ${type}`;
    box.textContent = msg;
}

function setBtnLoading(btn, isLoading) {
    if (!btn) return;
    btn.disabled = isLoading;
    const text = btn.querySelector(".btn-text");
    const spinner = btn.querySelector(".spinner");
    if (isLoading) {
        if (text) text.classList.add("hidden");
        if (spinner) spinner.classList.remove("hidden");
    } else {
        if (text) text.classList.remove("hidden");
        if (spinner) spinner.classList.add("hidden");
    }
}

function formatDuration(sec) {
    if (!sec || isNaN(sec)) return "--:--";
    const s = Math.floor(sec % 60);
    const m = Math.floor((sec / 60) % 60);
    const h = Math.floor(sec / 3600);
    if (h > 0) {
        return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }
    return `${m}:${String(s).padStart(2, '0')}`;
}

function showDrmNotice(box, data) {
    if (!box) return;
    box.className = "status-box show drm-warning";
    const platform = (data && data.platform) || "Protected Streaming Service";
    const title = (data && data.suggested_title) ? String(data.suggested_title).trim() : "";
    const cleanMsg = (data && (data.error || data.message)) || "This media is protected with proprietary DRM encryption.";

    let buttonHtml = "";
    if (title) {
        buttonHtml = `
            <button class="btn-drm-search" onclick="switchToSearchTab('${encodeURIComponent(title)}')">
                <span>🔍 Search &amp; Download "${escapeHtml(title)}" via Search Tab</span>
            </button>
        `;
    } else {
        buttonHtml = `
            <button class="btn-drm-search" onclick="switchToSearchTab('')">
                <span>🔍 Search Media by Title (Open Web &amp; YouTube)</span>
            </button>
        `;
    }

    box.innerHTML = `
        <div class="drm-notice-header">
            <span class="drm-notice-icon">🛡️</span>
            <span>Protected DRM Subscription Content &bull; ${escapeHtml(platform)}</span>
        </div>
        <div class="drm-notice-body">
            ${escapeHtml(cleanMsg)}
        </div>
        <div class="drm-notice-action">
            ${buttonHtml}
        </div>
    `;
}

function switchToSearchTab(encodedTitle) {
    const title = encodedTitle ? decodeURIComponent(encodedTitle) : "";

    // 1. Switch to Universal Downloader module if in another module
    const universalBtn = document.querySelector(`[onclick*="universal-module"]`);
    if (typeof switchModule === "function" && universalBtn) {
        switchModule("universal-module", universalBtn);
    }

    // 2. Activate the subtab-search tab
    document.querySelectorAll(".inner-tab-btn").forEach(btn => {
        if (btn.getAttribute("onclick") && btn.getAttribute("onclick").includes("subtab-search")) {
            btn.classList.add("active");
        } else {
            btn.classList.remove("active");
        }
    });
    document.querySelectorAll(".inner-tab-content").forEach(c => c.classList.remove("active"));
    const searchContent = document.getElementById("subtab-search");
    if (searchContent) searchContent.classList.add("active");

    // 3. Populate search input and auto-trigger ranked search
    const searchInput = document.getElementById("search-query");
    if (searchInput) {
        if (title) {
            searchInput.value = title;
            if (typeof runSearch === "function") {
                runSearch();
            }
        }
        searchInput.focus();
        searchInput.scrollIntoView({ behavior: "smooth", block: "center" });
    }
}

// =========================================================================
// 8. DIRECT CINEMA STREAM PLAYER (BYPASSES LOGIN & SUBSCRIPTION PAYWALLS)
// =========================================================================
// DIRECT CINEMA STREAMING (QUIDIAN ADSHIELD ZERO-ADS PRIVACY ENGINE)
// =========================================================================
let currentCinemaImdbId = "";
let currentCinemaTitle = "";
let currentCinemaUrl = "";
let cinemaServerIndex = 0;

// Global Quidian AdShield: Neutralize any rogue popup triggers or window unloads
(function initQuidianAdShield() {
    try {
        window.open = function(...args) {
            console.warn("[Quidian AdShield] Blocked popup attempt:", args[0] || "about:blank");
            return null;
        };
        // Block middle-click / auxiliary click popup attempts inside cinema modal
        window.addEventListener("auxclick", (e) => {
            if (e.target && e.target.closest && e.target.closest("#cinema-modal")) {
                e.preventDefault();
                e.stopPropagation();
            }
        }, true);
    } catch (e) {
        console.warn("[Quidian AdShield] Setup warning:", e);
    }
})();

const CINEMA_SERVERS = [
    {
        name: "Server 1 (VidLink Pro HD - Zero Ads)",
        makeUrl: (id, title) => {
            if (id && id.startsWith("tt")) return `https://vidlink.pro/movie/${id}`;
            return `https://vidlink.pro/movie/${encodeURIComponent(title)}`;
        }
    },
    {
        name: "Server 2 (AutoEmbed Multi-Source)",
        makeUrl: (id, title) => {
            if (id && id.startsWith("tt")) return `https://autoembed.co/movie/imdb/${id}`;
            return `https://autoembed.co/movie/imdb/${encodeURIComponent(title)}`;
        }
    },
    {
        name: "Server 3 (VidSrc PM Direct)",
        makeUrl: (id, title) => {
            if (id && id.startsWith("tt")) return `https://vidsrc.pm/embed/movie/${id}`;
            return `https://vidsrc.pm/embed/movie/${encodeURIComponent(title)}`;
        }
    },
    {
        name: "Server 4 (VidSrc TO Fast CDN)",
        makeUrl: (id, title) => {
            if (id && id.startsWith("tt")) return `https://vidsrc.to/embed/movie/${id}`;
            return `https://vidsrc.to/embed/movie/${encodeURIComponent(title)}`;
        }
    },
    {
        name: "Server 5 (SuperEmbed HD)",
        makeUrl: (id, title) => {
            if (id && id.startsWith("tt")) return `https://superembed.stream/?video_id=${id}`;
            return `https://superembed.stream/?video_id=${encodeURIComponent(title)}`;
        }
    }
];

function playDirectly(title, imdbId, platform, originalUrl) {
    currentCinemaTitle = (title || "Movie Stream").trim();
    currentCinemaImdbId = (imdbId || "").trim();
    currentCinemaUrl = decodeURIComponent(originalUrl || "");
    cinemaServerIndex = 0;

    const modal = document.getElementById("cinema-modal");
    const titleEl = document.getElementById("cinema-movie-title");
    if (titleEl) titleEl.textContent = currentCinemaTitle;

    loadCinemaServer(0);
    if (modal) modal.classList.remove("hidden");

    // If imdbId is not provided or not valid tt format, resolve via server proxy route (CORS safe)
    if ((!currentCinemaImdbId || !currentCinemaImdbId.startsWith("tt")) && currentCinemaTitle) {
        fetch(`/api/resolve_imdb?title=${encodeURIComponent(currentCinemaTitle)}`)
            .then(r => r.json())
            .then(data => {
                if (data && data.status === "success" && data.imdb_id) {
                    currentCinemaImdbId = data.imdb_id;
                    loadCinemaServer(cinemaServerIndex);
                }
            })
            .catch(() => {});
    }
}

function loadCinemaServer(index) {
    cinemaServerIndex = index % CINEMA_SERVERS.length;
    const srv = CINEMA_SERVERS[cinemaServerIndex];
    const iframe = document.getElementById("cinema-iframe");
    const serverLabel = document.getElementById("cinema-server-label");

    if (serverLabel) {
        serverLabel.textContent = srv.name;
    }

    if (iframe) {
        iframe.src = "about:blank";
        const targetUrl = srv.makeUrl(currentCinemaImdbId, currentCinemaTitle);
        setTimeout(() => {
            iframe.src = targetUrl;
        }, 50);
    }
}

function cycleCinemaServer() {
    loadCinemaServer(cinemaServerIndex + 1);
}

function closeCinemaModal() {
    const modal = document.getElementById("cinema-modal");
    const iframe = document.getElementById("cinema-iframe");
    if (iframe) iframe.src = "about:blank";
    if (modal) modal.classList.add("hidden");
}

function downloadFromCinemaModal() {
    closeCinemaModal();
    let downloadUrl = currentCinemaUrl;
    if (currentCinemaImdbId && currentCinemaImdbId.startsWith("tt")) {
        downloadUrl = `https://www.imdb.com/title/${currentCinemaImdbId}/`;
    }
    if (downloadUrl) {
        const urlInput = document.getElementById("video-url");
        if (urlInput) urlInput.value = downloadUrl;
        const universalBtn = document.querySelector(`[onclick*="universal-module"]`);
        if (typeof switchModule === "function" && universalBtn) {
            switchModule("universal-module", universalBtn);
        }
        downloadByLink();
    } else if (currentCinemaTitle) {
        switchToSearchTab(encodeURIComponent(currentCinemaTitle));
    }
}


