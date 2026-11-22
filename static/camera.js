(function () {
  'use strict';

  // ===== Constants & Config =====
  const LABEL_COLORS = {
    person: '#f85149',
    car: '#2f81f7', truck: '#2f81f7', bus: '#2f81f7', motorcycle: '#2f81f7', bicycle: '#2f81f7',
    cat: '#3fb950', dog: '#3fb950', bird: '#3fb950', horse: '#3fb950', sheep: '#3fb950', cow: '#3fb950', bear: '#3fb950',
    backpack: '#d29922', handbag: '#d29922', suitcase: '#d29922', umbrella: '#d29922'
  };
  const LABEL_CATEGORIES = {
    person: 'person',
    car: 'vehicle', truck: 'vehicle', bus: 'vehicle', motorcycle: 'vehicle', bicycle: 'vehicle',
    cat: 'animal', dog: 'animal', bird: 'animal', horse: 'animal', sheep: 'animal', cow: 'animal', bear: 'animal',
    backpack: 'other', handbag: 'other', suitcase: 'other', umbrella: 'other'
  };

  // ===== State =====
  let ws = null;
  let video = null;
  let overlay = null;
  let overlayCtx = null;
  let stream = null;
  let facingMode = 'environment';
  let cameraId = null;
  let cameraName = 'iPhone';
  let settings = {
    labels: ['person','car','truck','bus','motorcycle','bicycle','cat','dog','bird'],
    min_score: 0.55,
    cooldown_s: 30,
    preview_fps: 2,
    detect_fps: 2,
    mode: 'auto',
    retention_days: 14
  };
  let effectiveMode = 'starting'; // 'local' | 'server' | 'starting'
  let model = null;
  let tfReady = false;
  let detectionLoopTimer = null;
  let previewLoopTimer = null;
  let pingInterval = null;
  let reconnectAttempt = 0;
  let reconnectTimer = null;
  let wakeLock = null;
  let isDimmed = false;
  let isVisible = true;
  let running = false; // true when camera is started and not stopped
  let lastDetectionsSent = null; // for change detection in local mode
  let lastDetectionsSentTime = 0;
  let frameSentTimes = []; // timestamps of frames actually sent (for fps)
  let lastInferMs = 0;
  let recentInferTimes = [];
  let autoModeDecided = false;
  let pendingDetections = null;
  let frameW = 0;
  let frameH = 0;
  let displayScale = 1;
  let displayOffsetX = 0;
  let displayOffsetY = 0;
  let isFrontCamera = false;
  let detectionSrcW = 0;
  let detectionSrcH = 0;
  let resizeHandler = null;
  let orientationHandler = null;

  // ===== DOM Elements =====
  const statusDot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const modeBadge = document.getElementById('modeBadge');
  const fpsCounter = document.getElementById('fpsCounter');
  const inferCounter = document.getElementById('inferCounter');
  const labelsChip = document.getElementById('labelsChip');
  const videoEl = document.getElementById('video');
  const overlayEl = document.getElementById('overlay');
  const dimOverlay = document.getElementById('dimOverlay');
  const setupCard = document.getElementById('setupCard');
  const warningCard = document.getElementById('warningCard');
  const errorCard = document.getElementById('errorCard');
  const controls = document.getElementById('controls');
  const cameraNameInput = document.getElementById('cameraName');
  const startBtn = document.getElementById('startBtn');
  const dimBtn = document.getElementById('dimBtn');
  const switchBtn = document.getElementById('switchBtn');
  const stopBtn = document.getElementById('stopBtn');
  const wakeLockHint = document.getElementById('wakeLockHint');
  const viewfinder = document.getElementById('viewfinder');
  const toastContainer = document.getElementById('toastContainer');

  // ===== Utility =====
  function show(el) { el.classList.remove('hidden'); }
  function hide(el) { el.classList.add('hidden'); }
  function setStatus(connected, text) {
    statusDot.classList.toggle('connected', connected);
    statusDot.classList.toggle('connecting', !connected && text === 'Connecting…');
    statusText.textContent = text;
  }
  function setModeBadge(mode) {
    modeBadge.className = 'mode-badge ' + mode;
    const labels = { local: 'On-device AI', server: 'Server AI', starting: 'Starting…' };
    modeBadge.textContent = labels[mode] || mode;
  }
  function updateHUD() {
    // fps from frameSentTimes (last 2 seconds)
    const now = Date.now();
    const recent = frameSentTimes.filter(t => now - t <= 2000);
    const fps = recent.length / 2; // per second over 2s window
    fpsCounter.textContent = fps.toFixed(1) + ' fps';
    inferCounter.textContent = lastInferMs > 0 ? lastInferMs + ' ms' : '— ms';
    // labels chips
    labelsChip.replaceChildren();
    if (pendingDetections && pendingDetections.length) {
      const seen = new Set();
      pendingDetections.forEach(d => {
        if (d.score >= settings.min_score && settings.labels.includes(d.label) && !seen.has(d.label)) {
          seen.add(d.label);
          const chip = document.createElement('span');
          chip.className = 'label-chip ' + (LABEL_CATEGORIES[d.label] || 'other');
          chip.textContent = d.label;
          labelsChip.appendChild(chip);
        }
      });
    }
  }
  function showToast(message) {
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    toastContainer.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('show'));
    setTimeout(() => {
      toast.classList.remove('show');
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  }
  function showError(message) {
    errorCard.textContent = message;
    errorCard.classList.add('visible');
  }
  function hideError() {
    errorCard.classList.remove('visible');
  }

  // ===== LocalStorage =====
  function loadPersisted() {
    try {
      cameraId = localStorage.getItem('seccam_camera_id');
      if (!cameraId) {
        cameraId = 'iphone-' + Array.from(crypto.getRandomValues(new Uint8Array(4))).map(b => b.toString(16).padStart(2, '0')).join('');
        localStorage.setItem('seccam_camera_id', cameraId);
      }
      const name = localStorage.getItem('seccam_camera_name');
      if (name) cameraName = name;
      cameraNameInput.value = cameraName;
    } catch (e) {
      cameraId = 'iphone-' + Math.random().toString(16).slice(2, 10);
    }
  }
  function saveName() {
    try { localStorage.setItem('seccam_camera_name', cameraName); } catch (e) {}
  }

  // ===== Wake Lock =====
  async function requestWakeLock() {
    if ('wakeLock' in navigator) {
      try {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => { wakeLock = null; });
        wakeLockHint.textContent = '';
        return true;
      } catch (e) {
        wakeLockHint.textContent = 'Set Auto-Lock to Never in Settings > Display';
      }
    } else {
      wakeLockHint.textContent = 'Set Auto-Lock to Never in Settings > Display';
    }
    return false;
  }
  function releaseWakeLock() {
    if (wakeLock) { wakeLock.release(); wakeLock = null; }
  }
  document.addEventListener('visibilitychange', async () => {
    isVisible = !document.hidden;
    if (isVisible && running) {
      if (wakeLock === null) await requestWakeLock();
      setStatus(ws && ws.readyState === WebSocket.OPEN, 'Connected');
      // Reconnect WS if needed
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        connect();
      }
      resumeLoops();
    } else if (running) {
      setStatus(false, 'Paused (screen off / tab hidden)');
      pauseLoops();
    }
  });

  // ===== Dim Overlay =====
  function toggleDim() {
    isDimmed = !isDimmed;
    dimOverlay.classList.toggle('active', isDimmed);
    dimBtn.classList.toggle('dim-active', isDimmed);
    dimBtn.textContent = isDimmed ? 'Wake' : 'Dim';
  }
  dimOverlay.addEventListener('click', toggleDim);
  dimBtn.addEventListener('click', toggleDim);

  // ===== Canvas Overlay Drawing =====
  function resizeOverlay() {
    const vw = videoEl.videoWidth;
    const vh = videoEl.videoHeight;
    if (!vw || !vh) return;
    const containerRect = viewfinder.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const scale = Math.min(containerRect.width / vw, containerRect.height / vh);
    const dw = vw * scale;
    const dh = vh * scale;
    displayScale = scale;
    displayOffsetX = (containerRect.width - dw) / 2;
    displayOffsetY = (containerRect.height - dh) / 2;
    overlayEl.width = containerRect.width * dpr;
    overlayEl.height = containerRect.height * dpr;
    overlayEl.style.width = containerRect.width + 'px';
    overlayEl.style.height = containerRect.height + 'px';
    overlayCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function drawBoxes(detections, srcW, srcH) {
    if (!overlayCtx) return;
    const dpr = window.devicePixelRatio || 1;
    overlayCtx.clearRect(0, 0, overlayEl.width / dpr, overlayEl.height / dpr);
    if (!detections || !detections.length) return;
    // Scale from source (video or jpeg) to video coordinates
    const scaleToVideoX = videoEl.videoWidth / srcW;
    const scaleToVideoY = videoEl.videoHeight / srcH;
    detections.forEach(d => {
      if (d.score < settings.min_score) return;
      if (!settings.labels.includes(d.label)) return;
      const [x, y, w, h] = d.bbox;
      const color = LABEL_COLORS[d.label] || '#d29922';
      // Map to video coordinates
      const vx = x * scaleToVideoX;
      const vy = y * scaleToVideoY;
      const vw = w * scaleToVideoX;
      const vh = h * scaleToVideoY;
      // Map to display coordinates (with object-fit: contain)
      const dx = displayOffsetX + vx * displayScale;
      const dy = displayOffsetY + vy * displayScale;
      const dw = vw * displayScale;
      const dh = vh * displayScale;
      // Mirror for front camera
      let drawX = dx;
      if (isFrontCamera) {
        drawX = overlayEl.width / dpr - dx - dw;
      }
      overlayCtx.strokeStyle = color;
      overlayCtx.lineWidth = 2;
      overlayCtx.strokeRect(drawX, dy, dw, dh);
      overlayCtx.fillStyle = color;
      const label = d.label + ' ' + Math.round(d.score * 100) + '%';
      const fontSize = 12;
      overlayCtx.font = fontSize + 'px system-ui';
      const tw = overlayCtx.measureText(label).width + 8;
      const labelX = isFrontCamera ? drawX - tw : drawX;
      overlayCtx.fillRect(labelX, dy - fontSize - 4, tw, fontSize + 4);
      overlayCtx.fillStyle = '#fff';
      overlayCtx.fillText(label, labelX + 4, dy - 4);
    });
  }
  function attachResizeListeners() {
    resizeHandler = () => resizeOverlay();
    orientationHandler = () => setTimeout(resizeOverlay, 100);
    window.addEventListener('resize', resizeHandler);
    window.addEventListener('orientationchange', orientationHandler);
    videoEl.addEventListener('loadedmetadata', resizeOverlay);
  }
  function detachResizeListeners() {
    if (resizeHandler) window.removeEventListener('resize', resizeHandler);
    if (orientationHandler) window.removeEventListener('orientationchange', orientationHandler);
    videoEl.removeEventListener('loadedmetadata', resizeOverlay);
  }

  // ===== Frame Capture & Send =====
  let lastFrameSentAt = 0;
  function captureAndSend() {
    if (!stream || videoEl.readyState < 2) return;
    const vw = videoEl.videoWidth;
    const vh = videoEl.videoHeight;
    if (!vw || !vh) return;
    const maxW = 640;
    let dw = vw, dh = vh;
    if (dw > maxW) { dh = Math.round(dh * maxW / dw); dw = maxW; }
    // Create a fresh canvas each time to avoid races between preview and detection-triggered captures
    const canvas = document.createElement('canvas');
    canvas.width = dw;
    canvas.height = dh;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(videoEl, 0, 0, dw, dh);
    canvas.toBlob(blob => {
      if (!blob) return;
      const reader = new FileReader();
      reader.onload = () => {
        if (ws && ws.readyState === WebSocket.OPEN && ws.bufferedAmount < 256 * 1024) {
          ws.send(reader.result);
          lastFrameSentAt = Date.now();
          frameSentTimes.push(Date.now());
          // keep only last 2 seconds
          const cutoff = Date.now() - 2000;
          frameSentTimes = frameSentTimes.filter(t => t > cutoff);
        }
      };
      reader.readAsArrayBuffer(blob);
    }, 'image/jpeg', 0.6);
  }

  // ===== Detection Loop (Local Mode) =====
  // loopGen invalidates in-flight loop iterations on pause, so an inference that is still awaiting
  // cannot reschedule itself and start a duplicate loop.
  let loopGen = 0;
  async function runDetectionLoop(gen = loopGen) {
    if (gen !== loopGen || effectiveMode !== 'local' || !model || !running) return;
    if (videoEl.readyState < 2) {
      detectionLoopTimer = setTimeout(() => runDetectionLoop(gen), 100);
      return;
    }
    const start = performance.now();
    try {
      const predictions = await model.detect(videoEl, 20, 0.3);
      const inferMs = performance.now() - start;
      lastInferMs = Math.round(inferMs);
      recentInferTimes.push(inferMs);
      if (recentInferTimes.length > 5) recentInferTimes.shift();
      frameW = videoEl.videoWidth;
      frameH = videoEl.videoHeight;
      detectionSrcW = frameW;
      detectionSrcH = frameH;
      pendingDetections = predictions.map(p => ({
        label: p.class,
        score: p.score,
        bbox: p.bbox // [x, y, w, h]
      }));
      drawBoxes(pendingDetections, detectionSrcW, detectionSrcH);
      // Send detections to server if changed or every 2s
      const now = Date.now();
      const shouldSend = detectionsChanged(pendingDetections) || now - lastDetectionsSentTime >= 2000;
      if (shouldSend && ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'detections',
          detections: pendingDetections,
          frame_w: frameW,
          frame_h: frameH,
          infer_ms: Math.round(inferMs)
        }));
        lastDetectionsSent = pendingDetections.map(d => ({label: d.label, score: d.score, bbox: [...d.bbox]}));
        lastDetectionsSentTime = now;
      }
      // If any enabled detection above threshold, send frame immediately
      const shouldSendFrame = pendingDetections.some(d =>
        d.score >= settings.min_score && settings.labels.includes(d.label)
      );
      if (shouldSendFrame && Date.now() - lastFrameSentAt > 400) captureAndSend();
      // Auto mode: check first 5 inferences
      if (settings.mode === 'auto' && !autoModeDecided && recentInferTimes.length >= 5) {
        const avg = recentInferTimes.reduce((a, b) => a + b, 0) / recentInferTimes.length;
        if (avg > 1200) {
          await switchToServerMode('Auto: inference too slow (' + Math.round(avg) + 'ms avg)');
        } else {
          autoModeDecided = true;
        }
      }
    } catch (e) {
      console.error('Detection error:', e);
      if (settings.mode === 'auto' && !autoModeDecided) {
        await switchToServerMode('Auto: detection failed');
      }
    }
    if (gen === loopGen && running && effectiveMode === 'local') {
      const elapsed = performance.now() - start;
      const delay = Math.max(0, 1000 / settings.detect_fps - elapsed);
      detectionLoopTimer = setTimeout(() => runDetectionLoop(gen), delay);
    }
  }

  function detectionsChanged(newDets) {
    if (!lastDetectionsSent) return true;
    if (newDets.length !== lastDetectionsSent.length) return true;
    // Compare labels set and box positions
    const oldLabels = new Set(lastDetectionsSent.map(d => d.label));
    const newLabels = new Set(newDets.map(d => d.label));
    if (oldLabels.size !== newLabels.size || ![...oldLabels].every(l => newLabels.has(l))) return true;
    // Check each detection for >5% box movement
    for (let i = 0; i < newDets.length; i++) {
      const nd = newDets[i];
      const od = lastDetectionsSent.find(d => d.label === nd.label);
      if (!od) return true;
      const [nx, ny, nw, nh] = nd.bbox;
      const [ox, oy, ow, oh] = od.bbox;
      if (Math.abs(nx - ox) > 0.05 * frameW ||
          Math.abs(ny - oy) > 0.05 * frameH ||
          Math.abs(nw - ow) > 0.05 * frameW ||
          Math.abs(nh - oh) > 0.05 * frameH) {
        return true;
      }
    }
    return false;
  }

  // ===== Preview Loop =====
  function runPreviewLoop(gen = loopGen) {
    if (gen !== loopGen || !running) return;
    const rate = effectiveMode === 'server' ? Math.max(settings.preview_fps, settings.detect_fps) : settings.preview_fps;
    captureAndSend();
    previewLoopTimer = setTimeout(() => runPreviewLoop(gen), 1000 / rate);
  }

  function pauseLoops() {
    loopGen++;
    if (detectionLoopTimer) { clearTimeout(detectionLoopTimer); detectionLoopTimer = null; }
    if (previewLoopTimer) { clearTimeout(previewLoopTimer); previewLoopTimer = null; }
  }
  function resumeLoops() {
    const gen = loopGen;
    if (effectiveMode === 'local' && !detectionLoopTimer) detectionLoopTimer = setTimeout(() => runDetectionLoop(gen), 0);
    if (!previewLoopTimer) previewLoopTimer = setTimeout(() => runPreviewLoop(gen), 0);
  }

  // ===== TensorFlow.js / COCO-SSD =====
  async function loadModel() {
    if (tfReady) return true;
    const backends = ['webgl'];  // cpu/wasm too slow & wasm not vendored: auto mode falls back to server AI
    for (const backend of backends) {
      try {
        await tf.setBackend(backend);
        await tf.ready();
        model = await cocoSsd.load({ modelUrl: '/static/vendor/ssdlite_mobilenet_v2/model.json' });
        tfReady = true;
        return true;
      } catch (e) {
        console.warn('TF.js backend ' + backend + ' failed:', e);
        // Continue to next backend
      }
    }
    tfReady = false;
    return false;
  }
  function disposeModel() {
    if (model && model.dispose) { model.dispose(); model = null; }
    tfReady = false;
  }

  // ===== WebSocket =====
  function getWsUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + '/ws/camera';
  }
  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    if (!running) return;
    setStatus(false, 'Connecting…');
    ws = new WebSocket(getWsUrl());
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      reconnectAttempt = 0;
      setStatus(true, 'Connected');
      sendHello();
      sendMode();
      startPing();
    };
    ws.onclose = (e) => {
      stopPing();
      if (e.code === 4401) {
        setStatus(false, 'Session expired');
        location.href = '/login?next=/camera';
        return;
      }
      setStatus(false, 'Disconnected');
      if (running) scheduleReconnect();
    };
    ws.onerror = (e) => {
      console.error('WS error:', e);
      setStatus(false, 'Connection error');
    };
    ws.onmessage = (e) => handleMessage(e.data);
  }
  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempt), 15000) + Math.random() * 500;
    reconnectAttempt++;
    reconnectTimer = setTimeout(connect, delay);
  }
  function sendHello() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'hello', camera_id: cameraId, name: cameraName }));
    }
  }
  function sendMode() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'mode', mode: effectiveMode }));
    }
  }
  function startPing() {
    pingInterval = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
    }, 20000);
  }
  function stopPing() { if (pingInterval) clearInterval(pingInterval); pingInterval = null; }

  function handleMessage(data) {
    let msg;
    try { msg = JSON.parse(data); } catch { return; }
    switch (msg.type) {
      case 'config':
        const oldMode = settings.mode;
        settings = { ...settings, ...msg.settings };
        applySettings(oldMode);
        break;
      case 'detections':
        // Server-mode detections
        frameW = msg.frame_w;
        frameH = msg.frame_h;
        detectionSrcW = frameW;
        detectionSrcH = frameH;
        lastInferMs = msg.infer_ms || 0;
        pendingDetections = msg.detections || [];
        drawBoxes(pendingDetections, detectionSrcW, detectionSrcH);
        updateHUD();
        break;
      case 'error':
        showToast('Server error: ' + msg.message);
        break;
      case 'pong':
        break;
    }
  }

  function applySettings(oldMode) {
    // If mode changed, re-evaluate effective mode
    if (settings.mode !== oldMode) {
      if (settings.mode === 'server') {
        switchToServerMode('Server requested server mode');
      } else {
        // local or auto
        tryLocalMode();
      }
    }
    // Loops will pick up new fps settings automatically
  }

  // ===== Mode Switching =====
  async function switchToServerMode(reason) {
    if (effectiveMode === 'server') return;
    showToast('Using server AI: ' + reason);
    effectiveMode = 'server';
    disposeModel();
    setModeBadge('server');
    sendMode();
    showToast('Switched to server AI: ' + reason);
    pauseLoops();
    resumeLoops();
  }
  async function tryLocalMode() {
    if (effectiveMode === 'local') return;
    const loaded = await loadModel();
    if (!loaded) {
      if (settings.mode === 'auto') await switchToServerMode('Model load failed');
      return;
    }
    effectiveMode = 'local';
    autoModeDecided = false;
    recentInferTimes = [];
    setModeBadge('local');
    sendMode();
    pauseLoops();
    resumeLoops();
  }

  // ===== Camera Control =====
  async function startCamera() {
    hide(setupCard);
    hide(warningCard);
    hideError();
    show(controls);
    running = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          facingMode: { ideal: facingMode },
          width: { ideal: 640 },
          height: { ideal: 480 },
          frameRate: { ideal: 15, max: 15 }
        }
      });
      videoEl.srcObject = stream;
      await videoEl.play();
      await requestWakeLock();
      // Initialize overlay context
      overlayCtx = overlayEl.getContext('2d');
      attachResizeListeners();
      resizeOverlay();
      // Connect WS
      connect();
      // Start appropriate mode
      if (settings.mode === 'server') {
        effectiveMode = 'server';
        setModeBadge('server');
        sendMode();
        resumeLoops();
      } else {
        await tryLocalMode();
      }
    } catch (e) {
      console.error('Camera start failed:', e);
      running = false;
      show(setupCard);
      hide(controls);
      let msg = 'Camera access denied or not available.';
      if (e.name === 'NotAllowedError' || e.name === 'PermissionDeniedError') {
        msg = 'Camera permission denied. Enable in Settings > Safari > Camera.';
      } else if (e.name === 'NotFoundError') {
        msg = 'No camera found on this device.';
      } else if (e.name === 'NotReadableError') {
        msg = 'Camera is in use by another app.';
      }
      showError(msg);
    }
  }
  function stopCamera() {
    running = false;
    pauseLoops();
    stopPing();
    releaseWakeLock();
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) { ws.close(1000, 'stop'); ws = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    videoEl.srcObject = null;
    overlayCtx?.clearRect(0, 0, overlayEl.width, overlayEl.height);
    pendingDetections = null;
    detachResizeListeners();
    updateHUD();
    setStatus(false, 'Stopped');
    setModeBadge('starting');
    hide(controls);
    show(setupCard);
  }
  async function switchCamera() {
    if (!stream) return;
    facingMode = facingMode === 'environment' ? 'user' : 'environment';
    isFrontCamera = facingMode === 'user';
    const tracks = stream.getVideoTracks();
    tracks.forEach(t => t.stop());
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          facingMode: { ideal: facingMode },
          width: { ideal: 640 },
          height: { ideal: 480 },
          frameRate: { ideal: 15, max: 15 }
        }
      });
      videoEl.srcObject = stream;
      await videoEl.play();
      // Mirror front camera video
      videoEl.style.transform = isFrontCamera ? 'scaleX(-1)' : 'scaleX(1)';
      resizeOverlay();
    } catch (e) {
      console.error('Switch camera failed:', e);
      // Try to restore previous
      facingMode = facingMode === 'environment' ? 'user' : 'environment';
      isFrontCamera = facingMode === 'user';
    }
  }

  // ===== Secure Context Check =====
  function checkSecureContext() {
    if (!window.isSecureContext || !navigator.mediaDevices) {
      show(warningCard);
      hide(setupCard);
      startBtn.disabled = true;
    } else {
      hide(warningCard);
      show(setupCard);
      startBtn.disabled = false;
    }
  }

  // ===== FPS Counter (updates HUD every second) =====
  function updateFpsLoop() {
    updateHUD();
    setTimeout(updateFpsLoop, 1000);
  }

  // ===== Event Listeners =====
  startBtn.addEventListener('click', () => {
    cameraName = cameraNameInput.value.trim() || 'iPhone';
    saveName();
    startCamera();
  });
  stopBtn.addEventListener('click', stopCamera);
  switchBtn.addEventListener('click', switchCamera);
  cameraNameInput.addEventListener('change', () => {
    cameraName = cameraNameInput.value.trim() || 'iPhone';
    saveName();
    if (ws && ws.readyState === WebSocket.OPEN) sendHello();
  });

  // ===== Init =====
  loadPersisted();
  checkSecureContext();
  updateFpsLoop();

  // Handle page unload
  window.addEventListener('beforeunload', () => {
    stopCamera();
    disposeModel();
  });
})();
