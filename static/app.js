/**
 * Dalek Teleoperation WebRTC Client
 * Manages operator login, microphone capture, WebRTC signaling, and live visual VU meters.
 */

// State
let peerConnection = null;
let signalingSocket = null;
let localMediaStream = null;
let audioContext = null;
let localMeterNode = null;
let remoteMeterNode = null;
let meterAnimationId = null;
let isMuted = false;
let isConnected = false;

// DOM Elements
const authView = document.getElementById('auth-view');
const dashboardView = document.getElementById('dashboard-view');
const passwordInput = document.getElementById('password-input');
const loginBtn = document.getElementById('login-btn');
const authError = document.getElementById('auth-error');
const connectionChip = document.getElementById('connection-chip');
const stalkLight = document.getElementById('stalk-light');

const callToggleBtn = document.getElementById('call-toggle-btn');
const callBtnText = document.getElementById('call-btn-text');
const micMuteBtn = document.getElementById('mic-mute-btn');
const muteIcon = document.getElementById('mute-icon');
const muteText = document.getElementById('mute-text');
const logoutBtn = document.getElementById('logout-btn');

const outboundMeterFill = document.getElementById('outbound-meter-fill');
const inboundMeterFill = document.getElementById('inbound-meter-fill');
const remoteAudio = document.getElementById('remote-audio');


// ------------------------------------------------------------------ Init & Auth Flow
async function checkAuthStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    if (data.authenticated) {
      showDashboard();
    } else {
      showLogin();
    }
  } catch (e) {
    showLogin();
  }
}

function showLogin() {
  authView.classList.remove('hidden');
  dashboardView.classList.add('hidden');
  setConnectionStatus('DISCONNECTED', '');
}

function showDashboard() {
  authView.classList.add('hidden');
  dashboardView.classList.remove('hidden');
  setConnectionStatus('READY', '');
}

loginBtn.addEventListener('click', async () => {
  const password = passwordInput.value;
  if (!password) return;

  loginBtn.disabled = true;
  loginBtn.innerText = 'AUTHORIZING...';
  authError.classList.add('hidden');

  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password })
    });
    const data = await res.json();

    if (res.ok && data.success) {
      passwordInput.value = '';
      showDashboard();
    } else {
      authError.innerText = data.error || 'Access Denied';
      authError.classList.remove('hidden');
    }
  } catch (err) {
    authError.innerText = 'Failed to connect to Dalek service';
    authError.classList.remove('hidden');
  } finally {
    loginBtn.disabled = false;
    loginBtn.innerText = 'AUTHORIZE';
  }
});

logoutBtn.addEventListener('click', async () => {
  if (isConnected) {
    disconnectCall();
  }
  await fetch('/api/logout', { method: 'POST' });
  showLogin();
});


// ------------------------------------------------------------------ Status UI Updates
function setConnectionStatus(status, stateClass) {
  connectionChip.innerText = status;
  connectionChip.className = 'status-chip ' + stateClass;
  if (status === 'CONNECTED') {
    stalkLight.classList.add('active');
  } else {
    stalkLight.classList.remove('active');
  }
}


// ------------------------------------------------------------------ WebRTC Audio Comms
callToggleBtn.addEventListener('click', () => {
  if (isConnected) {
    disconnectCall();
  } else {
    connectCall();
  }
});

async function connectCall() {
  setConnectionStatus('CONNECTING...', 'connecting');
  callBtnText.innerText = 'CONNECTING...';
  callToggleBtn.disabled = true;

  try {
    // 1. Capture Phone Microphone with low latency & echo cancellation
    localMediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: 48000,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false
    });

    // 2. Setup Web Audio Context for visual audio meter
    setupAudioContext();

    // 3. Connect to WebSocket Signaling
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    signalingSocket = new WebSocket(`${protocol}//${window.location.host}/ws`);

    signalingSocket.onopen = async () => {
      // 4. Initialize RTCPeerConnection
      peerConnection = new RTCPeerConnection({
        iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
      });

      // Add local phone audio track
      localMediaStream.getAudioTracks().forEach(track => {
        peerConnection.addTrack(track, localMediaStream);
      });

      // Listen for incoming Dalek microphone track
      peerConnection.ontrack = (event) => {
        if (event.track.kind === 'audio') {
          remoteAudio.srcObject = event.streams[0];
          hookRemoteAudioMeter(event.streams[0]);
        }
      };

      peerConnection.onconnectionstatechange = () => {
        if (peerConnection.connectionState === 'connected') {
          isConnected = true;
          setConnectionStatus('CONNECTED', 'connected');
          callBtnText.innerText = 'END TRANSMISSION';
          callToggleBtn.className = 'giant-call-btn state-connected';
          callToggleBtn.disabled = false;
          micMuteBtn.disabled = false;
        } else if (peerConnection.connectionState === 'disconnected' || peerConnection.connectionState === 'failed') {
          disconnectCall();
        }
      };

      // Create WebRTC Offer
      const offer = await peerConnection.createOffer();
      await peerConnection.setLocalDescription(offer);

      signalingSocket.send(JSON.stringify({
        action: 'offer',
        sdp: offer.sdp,
        type: offer.type
      }));
    };

    signalingSocket.onmessage = async (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.action === 'answer') {
        await peerConnection.setRemoteDescription(new RTCSessionDescription({
          type: msg.type,
          sdp: msg.sdp
        }));
      }
    };

    signalingSocket.onerror = (err) => {
      console.error('Signaling socket error:', err);
      disconnectCall();
    };

  } catch (err) {
    console.error('Error starting call:', err);
    alert('Could not access microphone: ' + err.message);
    disconnectCall();
  }
}

function disconnectCall() {
  isConnected = false;
  callToggleBtn.disabled = false;
  micMuteBtn.disabled = true;
  callBtnText.innerText = 'TRANSMIT TO DALEK';
  callToggleBtn.className = 'giant-call-btn state-call';
  setConnectionStatus('READY', '');

  if (signalingSocket) {
    try {
      signalingSocket.send(JSON.stringify({ action: 'hangup' }));
      signalingSocket.close();
    } catch (e) {}
    signalingSocket = null;
  }

  if (peerConnection) {
    peerConnection.close();
    peerConnection = null;
  }

  if (localMediaStream) {
    localMediaStream.getTracks().forEach(t => t.stop());
    localMediaStream = null;
  }

  if (meterAnimationId) {
    cancelAnimationFrame(meterAnimationId);
    meterAnimationId = null;
  }

  outboundMeterFill.style.width = '0%';
  inboundMeterFill.style.width = '0%';
}


// ------------------------------------------------------------------ Mic Mute Toggle
micMuteBtn.addEventListener('click', () => {
  if (!localMediaStream) return;
  isMuted = !isMuted;
  localMediaStream.getAudioTracks().forEach(t => t.enabled = !isMuted);

  if (isMuted) {
    muteIcon.innerText = '🔇';
    muteText.innerText = 'Unmute Mic';
    micMuteBtn.style.backgroundColor = 'rgba(255, 51, 75, 0.2)';
  } else {
    muteIcon.innerText = '🎤';
    muteText.innerText = 'Mute Mic';
    micMuteBtn.style.backgroundColor = '';
  }
});


// ------------------------------------------------------------------ Audio Meters
function setupAudioContext() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  audioContext = new AudioContextClass();

  // Local mic analyser
  const localSource = audioContext.createMediaStreamSource(localMediaStream);
  localMeterNode = audioContext.createAnalyser();
  localMeterNode.fftSize = 256;
  localSource.connect(localMeterNode);

  updateAudioMeters();
}

function hookRemoteAudioMeter(stream) {
  if (!audioContext) return;
  try {
    const remoteSource = audioContext.createMediaStreamSource(stream);
    remoteMeterNode = audioContext.createAnalyser();
    remoteMeterNode.fftSize = 256;
    remoteSource.connect(remoteMeterNode);
  } catch (e) {
    console.warn('Could not hook remote audio meter:', e);
  }
}

function updateAudioMeters() {
  if (!isConnected && !localMediaStream) return;

  // Read local peak
  if (localMeterNode && !isMuted) {
    const pcmData = new Uint8Array(localMeterNode.frequencyBinCount);
    localMeterNode.getByteTimeDomainData(pcmData);
    let peak = 0;
    for (let i = 0; i < pcmData.length; i++) {
      const val = Math.abs(pcmData[i] - 128);
      if (val > peak) peak = val;
    }
    const percent = Math.min(100, (peak / 64) * 100);
    outboundMeterFill.style.width = percent + '%';
  } else {
    outboundMeterFill.style.width = '0%';
  }

  // Read remote peak
  if (remoteMeterNode) {
    const pcmData = new Uint8Array(remoteMeterNode.frequencyBinCount);
    remoteMeterNode.getByteTimeDomainData(pcmData);
    let peak = 0;
    for (let i = 0; i < pcmData.length; i++) {
      const val = Math.abs(pcmData[i] - 128);
      if (val > peak) peak = val;
    }
    const percent = Math.min(100, (peak / 64) * 100);
    inboundMeterFill.style.width = percent + '%';
  } else {
    inboundMeterFill.style.width = '0%';
  }

  meterAnimationId = requestAnimationFrame(updateAudioMeters);
}


// Start checking auth status on page load
checkAuthStatus();
