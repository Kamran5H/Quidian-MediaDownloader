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

/** Stop every per-card progress timer (called on new search / teardown). */
function clearSearchCardTimers() {
    for (const [, entry] of searchCardJobMap.entries()) {
        if (entry && entry.timer) clearInterval(entry.timer);
    }
    searchCardJobMap.clear();
}

window.addEventListener("beforeunload", () => {
    clearSearchCardTimers();
    if (activeProgressTimer) clearInterval(activeProgressTimer);
});

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
            // Deliberately leave the poller running: it reports the final
            // "cancelled" state once the worker actually unwinds.
            const statusBox = document.getElementById("link-status");
            const btn = document.getElementById("btn-download-video");
            setBtnLoading(btn, false);
            showStatus(statusBox, "Cancelling download - it will stop at the next safe point.", "info");
            const progTitle = document.getElementById("link-progress-title");
            if (progTitle) progTitle.textContent = "Cancelling...";
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

    let missCount = 0;
    const stop = () => {
        clearInterval(activeProgressTimer);
        activeProgressTimer = null;
    };

    activeProgressTimer = setInterval(async () => {
        let data;
        try {
            const res = await fetch(`/api/progress/${jobId}`);
            data = await res.json().catch(() => null);
            if (res.status === 404 || (data && data.status === "expired")) {
                // The job record has aged out of the registry - nothing more
                // will ever arrive, so stop rather than polling forever.
                stop();
                if (phaseEl) phaseEl.textContent = "\u23F3 Job expired";
                if (onComplete) onComplete("error", { error: "This job is no longer being tracked." });
                return;
            }
            if (!data) throw new Error("bad payload");
            missCount = 0;
        } catch {
            if (++missCount >= 20) {
                stop();
                if (phaseEl) phaseEl.textContent = "\u274C Connection lost";
                if (onComplete) onComplete("error", { error: "Lost contact with the local server." });
            }
            return;
        }

        if (data.cancel && data.status !== "cancelled") {
            // A cancel is in flight. With aria2c turbo the transfer cannot be
            // interrupted mid-stream, so say so rather than looking frozen.
            if (phaseEl) phaseEl.textContent = "\u23F9 Cancelling - stopping at the next safe point...";
            if (speedEl) speedEl.textContent = "";
            if (etaEl) etaEl.textContent = "";
        } else if (data.status === "downloading") {
            const pct = Math.min(100, Math.max(0, data.percent || 0)).toFixed(0);
            if (fillEl) fillEl.style.width = `${pct}%`;
            if (pctEl) pctEl.textContent = `${pct}%`;
            if (titleEl && data.title) titleEl.textContent = data.title;
            if (speedEl) speedEl.textContent = data.speed ? `\u26A1 ${data.speed}` : "\u26A1 Accelerating";
            if (etaEl) etaEl.textContent = data.eta ? `\u23F1 ETA ${formatDuration(data.eta)}` : "\u23F1 Estimating";
            if (phaseEl) phaseEl.textContent = data.phase ? `\uD83D\uDCE6 ${data.phase}` : "\uD83D\uDCE6 Stream active";
        } else if (data.status === "processing") {
            if (fillEl) fillEl.style.width = "98%";
            if (pctEl) pctEl.textContent = "98%";
            if (phaseEl) phaseEl.textContent = "\u2699\uFE0F Merging streams / converting";
            if (titleEl && data.title) titleEl.textContent = data.title;
        } else if (data.status === "done") {
            stop();
            if (fillEl) fillEl.style.width = "100%";
            if (pctEl) pctEl.textContent = "100%";
            if (phaseEl) phaseEl.textContent = "\u2705 Complete";
            if (titleEl && data.title) titleEl.textContent = data.title;
            if (onComplete) onComplete("done", data);
        } else if (data.status === "error") {
            stop();
            if (phaseEl) phaseEl.textContent = "\u274C Failed";
            if (onComplete) onComplete("error", data);
        } else if (data.status === "cancelled") {
            stop();
            if (phaseEl) phaseEl.textContent = "\u23F9 Cancelled";
            if (onComplete) onComplete("cancelled", data);
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
    clearSearchCardTimers();

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

// Set by renderSearchResults so "Download Best Match" always targets the top
// *playable* result. It used to grab the first .btn-card-action in the DOM,
// which is the "Play (No Login)" button whenever the top card is a stream card.
let topPlayableDownloadBtn = null;

function downloadBestMatch() {
    if (!topPlayableDownloadBtn || !document.body.contains(topPlayableDownloadBtn)) {
        showStatus(document.getElementById("search-status"),
            "No downloadable result in the current list - try a different query.", "info");
        return;
    }
    topPlayableDownloadBtn.click();
    topPlayableDownloadBtn.scrollIntoView({ behavior: "smooth", block: "center" });
}

function openStreamingPlatform(url, platformName) {
    const opened = openExternal(url);
    const statusBox = document.getElementById("search-status");
    if (!statusBox) return;
    if (opened) {
        showStatus(statusBox,
            `Opened ${platformName} in a new tab. Streaming services use DRM, so for an offline copy ` +
            `use the Download button on a YouTube, Dailymotion or Archive.org card.`, "info");
    } else {
        showStatus(statusBox,
            `Could not open ${platformName} - your browser blocked the new tab.`, "error");
    }
}

function filterResultsByPlatform(platform, chipEl) {
    document.querySelectorAll(".platform-filter-chip").forEach(c => c.classList.remove("active"));
    if (chipEl) chipEl.classList.add("active");

    const norm = s => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    const target = norm(platform);
    document.querySelectorAll(".media-result-card").forEach(c => {
        if (platform === "all") {
            c.style.display = "";
            return;
        }
        const cp = norm(c.dataset.platform);
        c.style.display = (cp === target || cp.includes(target) || target.includes(cp)) ? "" : "none";
    });
}

function buildPlatformFilterBar(counts, totalShown) {
    const bar = el("div", "platform-filter-bar");
    bar.appendChild(el("span", "platform-filter-label", "Sources:"));

    const makeChip = (label, count, value, active) => {
        const btn = el("button", "platform-filter-chip" + (active ? " active" : ""));
        btn.type = "button";
        btn.appendChild(document.createTextNode(label + " "));
        btn.appendChild(el("span", "chip-count", count));
        // Listener + dataset instead of an inline onclick string: a platform
        // name containing a quote used to break out of the handler attribute.
        btn.dataset.platform = value;
        btn.addEventListener("click", () => filterResultsByPlatform(value, btn));
        return btn;
    };

    bar.appendChild(makeChip("All Sources", totalShown, "all", true));
    Object.keys(counts).forEach(p => bar.appendChild(makeChip(p, counts[p], p, false)));
    return bar;
}

function buildResultCard(item, idx) {
    const isWeb = item.source === "web";
    const platform = item.platform || (item.source === "youtube" ? "YouTube"
        : (item.source === "dailymotion" ? "Dailymotion"
            : (item.source === "archive" ? "Archive.org" : (item.uploader || "Web"))));
    const platformClass = String(platform).toLowerCase().replace(/[^a-z0-9]/g, "");
    const isStreaming = !!item.is_streaming || ["netflix", "primevideo", "prime", "imdb"].includes(platformClass);
    const dur = (isWeb || isStreaming) ? "" : formatDuration(item.duration);
    const views = fmtViews(item.view_count);
    const isFullMovie = (item.duration && item.duration >= 2400) || item.badge === "Full Movie" || item.badge === "Archive Movie";
    const isFullVideo = !isFullMovie && ((item.duration && item.duration >= 1200) || item.badge === "Full Video");

    const card = el("div", "media-result-card" + (idx === 0 ? " top-pick" : ""));
    card.id = `media-card-${idx}`;
    card.dataset.platform = platform;

    // ---- thumbnail -------------------------------------------------------
    const thumb = el("div", "media-thumb");
    const thumbUrl = safeHttpUrl(item.thumbnail);
    if (thumbUrl) {
        const img = document.createElement("img");
        img.alt = "Thumbnail";
        img.loading = "lazy";
        img.referrerPolicy = "no-referrer";
        img.addEventListener("error", () => {
            img.remove();
            thumb.prepend(el("div", "media-thumb-fallback", isStreaming ? "STREAM" : "\u25B6"));
        }, { once: true });
        img.src = thumbUrl;   // assigned as a property, never interpolated into HTML
        thumb.appendChild(img);
    } else {
        thumb.appendChild(el("div", "media-thumb-fallback", isStreaming ? "STREAM" : (isWeb ? "WEB" : "\u25B6")));
    }
    if (dur && dur !== "--:--") thumb.appendChild(el("span", "media-dur", dur));
    if (idx === 0) thumb.appendChild(el("span", "top-pick-flag", "TOP PICK"));
    card.appendChild(thumb);

    // ---- meta ------------------------------------------------------------
    const meta = el("div", "media-meta");
    const titleEl = el("div", "media-title", item.title || "Untitled");
    titleEl.title = item.title || "";
    meta.appendChild(titleEl);

    const uploaderRow = el("div", "media-uploader");
    if (isFullMovie) {
        uploaderRow.appendChild(el("span", "full-movie-badge", "\uD83C\uDFAC Full Movie"));
    } else if (isFullVideo) {
        const b = el("span", "full-movie-badge", "\u23F1 Full Video");
        b.style.background = "rgba(99,102,241,0.2)";
        b.style.borderColor = "rgba(99,102,241,0.4)";
        b.style.color = "#a5b4fc";
        uploaderRow.appendChild(b);
    }
    if (item.badge && !["Full Movie", "Full Video", "Archive Movie"].includes(item.badge)) {
        uploaderRow.appendChild(el("span", "official-badge", item.badge));
    }
    if (item.stealth_protected) {
        const s = el("span", "official-badge", "\uD83D\uDEE1 Stealth");
        s.style.background = "rgba(168, 85, 247, 0.2)";
        s.style.color = "#c084fc";
        s.style.border = "1px solid rgba(168, 85, 247, 0.4)";
        uploaderRow.appendChild(s);
    }
    uploaderRow.appendChild(el("span", "uploader-name", item.uploader || item.channel || "Official"));

    let metaSubtitle = "";
    if (item.year) metaSubtitle = String(item.year);
    else if (views) metaSubtitle = views;
    else if (isStreaming) metaSubtitle = "Streaming";
    if (metaSubtitle) uploaderRow.appendChild(el("span", "view-count", "\u2022 " + metaSubtitle));

    uploaderRow.appendChild(el("span", `platform-source-tag ${platformClass}`, "\u2022 " + platform));
    meta.appendChild(uploaderRow);
    card.appendChild(meta);

    // ---- actions ---------------------------------------------------------
    const actionWrap = el("div", "media-action");
    let downloadBtn;
    if (isStreaming) {
        const group = el("div", "media-action-group");
        const cleanTitle = String(item.title || "").replace(/\s*\((Watch on|IMDb|Netflix|Prime).*?\)/gi, "").trim();

        const playBtn = el("button", "btn-card-action play-action", "\u25B6 Play (No Login)");
        playBtn.type = "button";
        playBtn.addEventListener("click", () => playDirectly(cleanTitle, item.imdb_id || "", platformClass, item.url));
        group.appendChild(playBtn);

        downloadBtn = el("button", "btn-card-action", "\u26A1 Download");
        downloadBtn.type = "button";
        downloadBtn.id = `btn-result-${idx}`;
        group.appendChild(downloadBtn);
        actionWrap.appendChild(group);
    } else {
        downloadBtn = el("button", "btn-card-action", "Download");
        downloadBtn.type = "button";
        downloadBtn.id = `btn-result-${idx}`;
        actionWrap.appendChild(downloadBtn);
    }
    downloadBtn.addEventListener("click", () => downloadSearchResult(item.url, idx));
    card.appendChild(actionWrap);

    // ---- inline progress slot -------------------------------------------
    const slot = el("div", "card-progress-slot hidden");
    slot.id = `card-prog-${idx}`;
    const track = el("div", "progress-track");
    track.style.marginTop = "0.5rem";
    const fill = el("div", "progress-bar-fill");
    fill.id = `card-fill-${idx}`;
    track.appendChild(fill);
    slot.appendChild(track);
    const progMeta = el("div", "card-prog-meta");
    const pct = el("span", "card-pct", "0%");
    pct.id = `card-pct-${idx}`;
    const spd = el("span", "card-speed");
    spd.id = `card-speed-${idx}`;
    progMeta.appendChild(pct);
    progMeta.appendChild(spd);
    slot.appendChild(progMeta);
    card.appendChild(slot);

    return { card, downloadBtn, isStreaming };
}

function renderSearchResults(items, cleanedQuery, originalQuery, platformCounts) {
    const grid = document.getElementById("search-results");
    if (!grid) return;
    grid.innerHTML = "";
    topPlayableDownloadBtn = null;

    if (cleanedQuery && originalQuery &&
        cleanedQuery.trim().toLowerCase() !== originalQuery.trim().toLowerCase()) {
        const bar = el("div", "search-cleaning-notice");
        bar.appendChild(el("span", null,
            `Platform filter detected: searching for "${cleanedQuery}" across all sources simultaneously.`));
        grid.appendChild(bar);
    }

    const counts = platformCounts || {};
    if (Object.keys(counts).length > 1) {
        grid.appendChild(buildPlatformFilterBar(counts, items.length));
    }

    const built = items.map((item, idx) => {
        const b = buildResultCard(item, idx);
        return b;
    });

    // "Best match" bar needs the button reference, so build it after the cards.
    const firstPlayable = built.find(b => !b.isStreaming);
    if (firstPlayable) {
        topPlayableDownloadBtn = firstPlayable.downloadBtn;
        const topItem = items[built.indexOf(firstPlayable)];
        const bar = el("div", "best-match-bar");
        const info = el("div", "best-match-info");
        info.appendChild(el("span", "best-match-star", "\u26A1"));
        const label = el("span");
        label.appendChild(document.createTextNode("Top Playable Match: "));
        label.appendChild(el("strong", null, String(topItem.title || "").slice(0, 55)));
        label.appendChild(document.createTextNode(` (${topItem.platform || "Direct"})`));
        info.appendChild(label);
        bar.appendChild(info);
        const btn = el("button", "btn-best-match", "Download Best Match");
        btn.type = "button";
        btn.addEventListener("click", downloadBestMatch);
        bar.appendChild(btn);
        grid.appendChild(bar);
    }

    built.forEach(b => grid.appendChild(b.card));
}

function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"'`=\/]/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        "`": "&#96;", "=": "&#61;", "/": "&#47;"
    }[c]));
}

/**
 * Only http(s) URLs are ever assigned to an <img src> or opened in a tab.
 * Search results come from third-party engines, so a javascript:/data: URL
 * must never reach the DOM.
 */
function safeHttpUrl(u) {
    if (typeof u !== "string" || !u) return "";
    try {
        const parsed = new URL(u, window.location.origin);
        return (parsed.protocol === "http:" || parsed.protocol === "https:") ? parsed.href : "";
    } catch {
        return "";
    }
}

/** Create an element with text content - never HTML - plus optional props. */
function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
}

function downloadSearchResult(url, idx) {
    requestDownloadWithDestination((chosenPath) => {
        executeDownloadSearchResult(url, idx, chosenPath);
    });
}

async function executeDownloadSearchResult(url, idx, outputPath) {
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

    // Clear any timer still attached to this card from a previous attempt.
    const prior = searchCardJobMap.get(idx);
    if (prior && prior.timer) clearInterval(prior.timer);

    if (btn) {
        btn.textContent = useStealth ? "Bypassing locks..." : "Connecting...";
        btn.disabled = true;
    }
    if (progSlot) progSlot.classList.remove("hidden");
    if (pctEl) pctEl.textContent = "0%";

    const resetToDownload = (label) => {
        if (!btn) return;
        btn.textContent = label;
        btn.className = "btn-card-action";
        btn.disabled = false;
        btn.onclick = null;
        btn.replaceWith(btn.cloneNode(true));
        const fresh = document.getElementById(`btn-result-${idx}`);
        if (fresh) fresh.addEventListener("click", () => downloadSearchResult(url, idx));
    };

    try {
        const res = await fetch("/api/download_video", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, quality, use_turbo: useTurbo, use_stealth: useStealth, output_path: outputPath })
        });
        const data = await res.json().catch(() => ({}));

        if (!res.ok || !data.job_id) {
            if (btn) { btn.textContent = "Error"; btn.disabled = true; }
            showStatus(document.getElementById("search-status"),
                data.error || "Could not start that download.", "error");
            return;
        }

        const jobId = data.job_id;
        const cardEl = document.getElementById(`media-card-${idx}`);
        if (cardEl) cardEl.dataset.activeJobId = jobId;

        if (btn) {
            btn.disabled = false;
            btn.textContent = "Cancel";
            btn.classList.add("cancel-mode");
            btn.onclick = () => cancelSearchCardJob(idx, jobId);
        }

        let missCount = 0;
        const timer = setInterval(async () => {
            const currentCard = document.getElementById(`media-card-${idx}`);
            if (!currentCard || currentCard.dataset.activeJobId !== jobId) {
                clearInterval(timer);
                searchCardJobMap.delete(idx);
                return;
            }
            let j;
            try {
                const pr = await fetch(`/api/progress/${jobId}`);
                j = await pr.json().catch(() => null);
                if (pr.status === 404 || (j && j.status === "expired")) {
                    // The job record aged out; stop polling instead of
                    // hammering the endpoint for the rest of the session.
                    clearInterval(timer);
                    searchCardJobMap.delete(idx);
                    if (pctEl) pctEl.textContent = "Expired";
                    resetToDownload("Download");
                    return;
                }
                if (!j) throw new Error("bad payload");
                missCount = 0;
            } catch {
                // Tolerate a handful of transient failures, then give up.
                if (++missCount >= 20) {
                    clearInterval(timer);
                    searchCardJobMap.delete(idx);
                    if (pctEl) pctEl.textContent = "Connection lost";
                    resetToDownload("Retry");
                }
                return;
            }

            if (j.cancel && j.status !== "cancelled") {
                if (pctEl) pctEl.textContent = "Cancelling...";
                if (speedElC) speedElC.textContent = "";
            } else if (j.status === "downloading") {
                const pct = Math.min(100, Math.max(0, j.percent || 0)).toFixed(0);
                if (fill) fill.style.width = `${pct}%`;
                if (pctEl) pctEl.textContent = `${pct}%  ${j.phase ? "\u00B7 " + j.phase : ""}`;
                if (speedElC) speedElC.textContent = j.speed || "";
            } else if (j.status === "processing") {
                if (fill) fill.style.width = "98%";
                if (pctEl) pctEl.textContent = "98% \u00B7 finalizing";
                if (speedElC) speedElC.textContent = "";
            } else if (j.status === "done") {
                clearInterval(timer);
                searchCardJobMap.delete(idx);
                if (fill) fill.style.width = "100%";
                if (pctEl) pctEl.textContent = "100% \u00B7 done";
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
                searchCardJobMap.delete(idx);
                if (fill) fill.style.width = "0%";
                if (pctEl) pctEl.textContent = "Cancelled";
                if (speedElC) speedElC.textContent = "";
                resetToDownload("Download");
            } else if (j.status === "error") {
                clearInterval(timer);
                searchCardJobMap.delete(idx);
                if (pctEl) pctEl.textContent = "Failed";
                if (speedElC) speedElC.textContent = "";
                resetToDownload("Retry");
                showStatus(document.getElementById("search-status"),
                    j.error || "Download failed.", "error");
            }
        }, 650);

        searchCardJobMap.set(idx, { jobId, timer });
    } catch (err) {
        if (btn) { btn.textContent = "Error"; btn.disabled = true; }
        showStatus(document.getElementById("search-status"),
            "Connection failed: " + err.message, "error");
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
    const urls = Array.from(checks)
        .map(c => c.getAttribute("data-url"))
        .filter(u => typeof u === "string" && /^https?:\/\//i.test(u));

    if (urls.length === 0) {
        showStatus(statusBox, "None of the selected items have a usable link.", "error");
        return;
    }

    const quality = document.getElementById("playlist-quality")?.value || "1080p";
    showStatus(statusBox, `Enqueuing batch download for ${urls.length} item(s) to ${outputPath}...`, "info");

    try {
        const res = await fetch("/api/playlist/download", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ urls, quality, output_path: outputPath, use_turbo: true, use_stealth: true })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.job_ids || data.job_ids.length === 0) {
            showStatus(statusBox, data.error || "Batch queue failed.", "error");
            return;
        }

        const jobIds = data.job_ids;
        const skippedNote = data.skipped ? ` (${data.skipped} skipped)` : "";
        showStatus(statusBox, `\u23F3 Downloading batch: 0 of ${jobIds.length} completed...${skippedNote}`, "info");

        let missCount = 0;
        const pollInterval = setInterval(async () => {
            let payload;
            try {
                // One bulk request per tick. Polling each job individually meant
                // 250 HTTP requests every 1.5s for a full playlist.
                const pr = await fetch("/api/progress_bulk", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ job_ids: jobIds })
                });
                payload = await pr.json().catch(() => null);
                if (!payload || !payload.counts) throw new Error("bad payload");
                missCount = 0;
            } catch {
                if (++missCount >= 20) {
                    clearInterval(pollInterval);
                    showStatus(statusBox, "Lost contact with the local server during the batch.", "error");
                }
                return;
            }

            const counts = payload.counts;
            const done = counts.done || 0;
            const failed = (counts.error || 0) + (counts.cancelled || 0) + (counts.expired || 0);
            if (done + failed >= jobIds.length) {
                clearInterval(pollInterval);
                const kind = failed === 0 ? "success" : (done === 0 ? "error" : "info");
                showStatus(statusBox,
                    `Batch complete: ${done} downloaded, ${failed} failed. Saved to ${outputPath}.`, kind);
                loadLibrary();
            } else {
                showStatus(statusBox,
                    `\u23F3 Downloading batch: ${done} of ${jobIds.length} completed${failed ? `, ${failed} failed` : ""}...`,
                    "info");
            }
        }, 1500);
    } catch (err) {
        showStatus(statusBox, "Batch error: " + err.message, "error");
    }
}

function pollModuleJob(jobId, statusBox, btn, defaultSuccessMsg, onDone) {
    setBtnLoading(btn, true);
    let missCount = 0;
    const timer = setInterval(async () => {
        let data;
        try {
            const res = await fetch(`/api/progress/${jobId}`);
            data = await res.json().catch(() => null);
            if (res.status === 404 || (data && data.status === "expired")) {
                clearInterval(timer);
                setBtnLoading(btn, false);
                showStatus(statusBox, "This job is no longer being tracked.", "error");
                return;
            }
            if (!data) throw new Error("bad payload");
            missCount = 0;
        } catch {
            if (++missCount >= 20) {
                clearInterval(timer);
                setBtnLoading(btn, false);
                showStatus(statusBox, "Lost contact with the local server.", "error");
            }
            return;
        }

        if (data.status === "downloading" || data.status === "processing") {
            const msg = data.message || "Processing media...";
            const pct = (data.percent && data.percent > 0) ? ` (${data.percent.toFixed(0)}%)` : "";
            showStatus(statusBox, `\u23F3 ${msg}${pct}`, "info");
        } else if (data.status === "done") {
            clearInterval(timer);
            setBtnLoading(btn, false);
            showStatus(statusBox, `\u2705 ${data.message || defaultSuccessMsg || "Completed successfully!"}`, "success");
            loadLibrary();
            if (typeof onDone === "function") onDone(data);
        } else if (data.status === "error") {
            clearInterval(timer);
            setBtnLoading(btn, false);
            showStatus(statusBox, `\u274C ${data.error || "Operation failed."}`, "error");
        } else if (data.status === "cancelled") {
            clearInterval(timer);
            setBtnLoading(btn, false);
            showStatus(statusBox, "Operation was cancelled by user.", "info");
        }
    }, 750);
    return timer;
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
        const data = await res.json().catch(() => null);

        if (!res.ok || !data || !Array.isArray(data.files)) {
            grid.innerHTML = "";
            grid.appendChild(el("p", null, (data && data.error) || "Could not load library contents."))
                .style.color = "#f87171";
            return;
        }

        if (pathLabel) pathLabel.textContent = `Location: ${data.directory} (${data.files.length} items)`;
        grid.innerHTML = "";

        if (data.files.length === 0) {
            const empty = el("p", null, "No media files found in this folder yet.");
            empty.style.color = "var(--text-dim)";
            empty.style.fontSize = "0.9rem";
            empty.style.gridColumn = "1/-1";
            grid.appendChild(empty);
            return;
        }

        data.files.forEach(file => grid.appendChild(buildLibraryCard(file)));
    } catch {
        grid.innerHTML = "";
        const p = el("p", null, "Could not load library contents.");
        p.style.color = "#f87171";
        grid.appendChild(p);
    }
}

function buildLibraryCard(file) {
    const card = el("div", "library-card");

    const top = el("div", "library-card-top");
    const name = el("div", "lib-file-name", file.name);
    name.title = file.path || file.name;
    top.appendChild(name);
    top.appendChild(el("span", "lib-ext-badge", file.ext || file.type || "MEDIA"));
    card.appendChild(top);

    const metaRow = el("div", "lib-meta-row");
    metaRow.appendChild(el("span", null, file.size_formatted || (file.size_mb ? `${file.size_mb} MB` : "")));
    metaRow.appendChild(el("span", null, file.modified || file.date || ""));
    card.appendChild(metaRow);

    const actions = el("div");
    actions.style.display = "flex";
    actions.style.gap = "0.5rem";
    actions.style.marginTop = "0.6rem";

    const playBtn = el("button", "btn-lib-action", "\u25B6 Play / Open");
    playBtn.type = "button";
    playBtn.style.flex = "1.2";
    playBtn.addEventListener("click", () => openLibraryFile(file.path));
    actions.appendChild(playBtn);

    const revealBtn = el("button", "btn-lib-action", "\uD83D\uDCC1 Reveal");
    revealBtn.type = "button";
    revealBtn.style.flex = "0.8";
    revealBtn.style.background = "rgba(255,255,255,0.07)";
    revealBtn.style.borderColor = "rgba(255,255,255,0.15)";
    revealBtn.title = "Highlight in the file manager";
    revealBtn.addEventListener("click", () => revealLibraryFile(file.path));
    actions.appendChild(revealBtn);

    card.appendChild(actions);
    return card;
}

async function libraryAction(endpoint, path, failureLabel) {
    try {
        const res = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path, file_path: path })
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            // A blocked executable or a deleted file used to fail silently.
            alert(data.error || failureLabel);
        }
    } catch (err) {
        alert(`${failureLabel} (${err.message})`);
    }
}

function openLibraryFile(path) {
    libraryAction("/api/library/open", path, "Could not open that file.");
}

function revealLibraryFile(path) {
    libraryAction("/api/library/reveal", path, "Could not reveal that file.");
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

    box.innerHTML = "";

    const header = el("div", "drm-notice-header");
    header.appendChild(el("span", "drm-notice-icon", "\uD83D\uDEE1\uFE0F"));
    header.appendChild(el("span", null, `Protected DRM Subscription Content \u2022 ${platform}`));
    box.appendChild(header);

    box.appendChild(el("div", "drm-notice-body", cleanMsg));

    const action = el("div", "drm-notice-action");
    const btn = el("button", "btn-drm-search");
    btn.type = "button";
    btn.appendChild(el("span", null, title
        ? `\uD83D\uDD0D Search & Download "${title}" via Search Tab`
        : "\uD83D\uDD0D Search Media by Title (Open Web & YouTube)"));
    // Bound as a listener, so a title containing quotes cannot escape into markup.
    btn.addEventListener("click", () => switchToSearchTab(title));
    action.appendChild(btn);
    box.appendChild(action);
}

function switchToSearchTab(rawTitle) {
    const title = rawTitle ? String(rawTitle) : "";

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

// Global Quidian AdShield.
//
// The previous build replaced window.open with a stub that returned null for
// everything, which silently broke the app's own "open on Netflix / Prime"
// buttons. Popups are now blocked only while the cinema modal is on screen,
// and only when the app itself is not the caller.
let adShieldAllowNextOpen = false;

(function initQuidianAdShield() {
    try {
        const nativeOpen = window.open.bind(window);
        window.open = function (...args) {
            const cinemaOpen = !!document.querySelector("#cinema-modal:not(.hidden)");
            if (adShieldAllowNextOpen || !cinemaOpen) {
                adShieldAllowNextOpen = false;
                return nativeOpen(...args);
            }
            console.warn("[Quidian AdShield] Blocked popup from embedded player:", args[0] || "about:blank");
            return null;
        };
        // Block middle-click / auxiliary-click popups inside the cinema modal.
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

/** Open a URL in a new tab, bypassing AdShield for app-initiated navigation. */
function openExternal(url) {
    const safe = safeHttpUrl(url);
    if (!safe) return false;
    adShieldAllowNextOpen = true;
    const win = window.open(safe, "_blank", "noopener,noreferrer");
    adShieldAllowNextOpen = false;
    if (win) win.opener = null;
    return !!win;
}

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
    currentCinemaUrl = safeHttpUrl(originalUrl || "");
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
        // These are untrusted third-party embeds: keep them sandboxed and
        // referrer-free, and never let them read the parent origin.
        iframe.setAttribute("referrerpolicy", "no-referrer");
        iframe.setAttribute("sandbox", "allow-scripts allow-same-origin allow-presentation allow-fullscreen");
        const targetUrl = safeHttpUrl(srv.makeUrl(currentCinemaImdbId, currentCinemaTitle));
        if (!targetUrl) return;
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
        switchToSearchTab(currentCinemaTitle);
    }
}


