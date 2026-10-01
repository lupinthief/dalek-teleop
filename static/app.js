/**
 * Dalek Teleoperation WebRTC Client
 * Manages operator login, live webcam video feed, full-duplex room audio,
 * push-to-transmit phone voice, and live visual audio meters.
 */

// State
let peerConnection = null;
let signalingSocket = null;
let audioTransceiver = null;
let localMediaStream = null;
let audioContext = null;
let localMeterNode = null;
let remoteMeterNode = null;
let meterAnimationId = null;
let wakeLock = null;
let noSleep = null;
let isMuted = false;
let isTransmitting = false;
let isConnected = false;

// DOM Elements
const authView = document.getElementById('auth-view');
const dashboardView = document.getElementById('dashboard-view');
const passwordInput = document.getElementById('password-input');
const loginBtn = document.getElementById('login-btn');
const authError = document.getElementById('auth-error');
const connectionChip = document.getElementById('connection-chip');
const stalkLight = document.getElementById('stalk-light');

const cameraFeed = document.getElementById('camera-feed');
const videoStatusText = document.getElementById('video-status-text');
const liveDot = document.getElementById('live-dot');

const callToggleBtn = document.getElementById('call-toggle-btn');
const callBtnIcon = document.getElementById('call-btn-icon');
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
  disconnectComms();
  authView.classList.remove('hidden');
  dashboardView.classList.add('hidden');
  setConnectionStatus('DISCONNECTED', '');
}

function showDashboard() {
  authView.classList.add('hidden');
  dashboardView.classList.remove('hidden');
  setConnectionStatus('INITIALIZING...', 'connecting');
  // Connect WebRTC automatically so live video feed starts immediately after auth
  connectComms();
}

loginBtn.addEventListener('click', async () => {
  const password = passwordInput.value;
  if (!password) return;

  // Unlock audio playback immediately on user tap so incoming audio is allowed to play
  if (remoteAudio) {
    remoteAudio.play().catch(() => {});
  }
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!audioContext) {
    audioContext = new AudioContextClass();
    if (audioContext.state === 'suspended') {
      audioContext.resume().catch(() => {});
    }
  }

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
  disconnectComms();
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


// ------------------------------------------------------------------ WebRTC Comms (Video + Audio)
async function connectComms() {
  if (peerConnection) return;

  setConnectionStatus('CONNECTING...', 'connecting');
  videoStatusText.innerText = 'CONNECTING FEED...';
  liveDot.classList.remove('active');

  try {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    signalingSocket = new WebSocket(`${protocol}//${window.location.host}/ws`);

    signalingSocket.onopen = async () => {
      peerConnection = new RTCPeerConnection({
        iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
      });

      // Prepare transceivers: receive video from camera and send/recv audio
      peerConnection.addTransceiver('video', { direction: 'recvonly' });
      audioTransceiver = peerConnection.addTransceiver('audio', { direction: 'sendrecv' });

      // Handle incoming remote media tracks (Dalek camera + Dalek room microphone)
      peerConnection.ontrack = (event) => {
        if (event.track.kind === 'video') {
          console.log('Received Dalek camera video track');
          cameraFeed.srcObject = event.streams[0] || new MediaStream([event.track]);
          cameraFeed.play().catch(e => console.log('Autoplay muted video:', e));
          videoStatusText.innerText = 'LIVE OPTICAL FEED';
          liveDot.classList.add('active');
        } else if (event.track.kind === 'audio') {
          console.log('Received Dalek room audio track');
          const stream = event.streams[0] || new MediaStream([event.track]);
          remoteAudio.srcObject = stream;
          remoteAudio.play().catch(e => {
            console.log('Audio autoplay blocked by mobile browser until user tap:', e);
          });
          hookRemoteAudioMeter(stream);
        }
      };

      peerConnection.onconnectionstatechange = () => {
        console.log('PeerConnection state:', peerConnection.connectionState);
        if (peerConnection.connectionState === 'connected') {
          isConnected = true;
          setConnectionStatus('CONNECTED', 'connected');
        } else if (peerConnection.connectionState === 'failed') {
          setConnectionStatus('RECONNECTING...', 'connecting');
          disconnectComms();
          setTimeout(connectComms, 2000);
        }
      };

      // Create and send SDP Offer
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
    };

    signalingSocket.onclose = () => {
      console.log('Signaling socket closed');
    };

  } catch (err) {
    console.error('Error starting WebRTC comms:', err);
    videoStatusText.innerText = 'FEED ERROR';
  }
}

function disconnectComms() {
  stopTransmitting();
  isConnected = false;
  setConnectionStatus('DISCONNECTED', '');
  videoStatusText.innerText = 'STANDBY';
  liveDot.classList.remove('active');

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
    audioTransceiver = null;
  }

  cameraFeed.srcObject = null;
  remoteAudio.srcObject = null;
}


// ------------------------------------------------------------------ Voice Transmit Toggle
callToggleBtn.addEventListener('click', async () => {
  // Ensure remote audio playback is unlocked on user interaction (required by iOS Safari/Chrome autoplay policy)
  if (remoteAudio && remoteAudio.paused) {
    remoteAudio.play().catch(e => console.log('Unlock remote audio:', e));
  }

  if (isTransmitting) {
    stopTransmitting();
  } else {
    // Enable keep-alive during active voice transmission
    enableScreenKeepAlive();
    await startTransmitting();
  }
});

async function startTransmitting() {
  if (!peerConnection) {
    await connectComms();
  }

  try {
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

    setupAudioContext();

    const audioTrack = localMediaStream.getAudioTracks()[0];
    if (audioTransceiver && audioTransceiver.sender) {
      await audioTransceiver.sender.replaceTrack(audioTrack);
    } else {
      const audioSender = peerConnection.getSenders().find(s => s.track && s.track.kind === 'audio');
      if (audioSender) {
        await audioSender.replaceTrack(audioTrack);
      } else {
        peerConnection.addTrack(audioTrack, localMediaStream);
      }
    }

    isTransmitting = true;
    callToggleBtn.classList.add('transmitting');
    callBtnIcon.innerText = '🛑';
    callBtnText.innerText = 'Stop';
    micMuteBtn.disabled = false;

  } catch (err) {
    console.error('Error accessing microphone:', err);
    alert('Could not access microphone: ' + err.message);
    disableScreenKeepAlive();
  }
}

async function stopTransmitting() {
  isTransmitting = false;
  callToggleBtn.classList.remove('transmitting');
  callBtnIcon.innerText = '🎙️';
  callBtnText.innerText = 'Transmit';
  micMuteBtn.disabled = true;

  if (peerConnection) {
    if (audioTransceiver && audioTransceiver.sender) {
      try {
        await audioTransceiver.sender.replaceTrack(null);
      } catch (e) {}
    }
  }

  if (localMediaStream) {
    localMediaStream.getTracks().forEach(t => t.stop());
    localMediaStream = null;
  }

  if (meterAnimationId) {
    cancelAnimationFrame(meterAnimationId);
    meterAnimationId = null;
  }

  disableScreenKeepAlive();
  outboundMeterFill.style.width = '0%';
}


// ------------------------------------------------------------------ Mic Mute Toggle
micMuteBtn.addEventListener('click', () => {
  if (!localMediaStream) return;
  isMuted = !isMuted;
  localMediaStream.getAudioTracks().forEach(t => t.enabled = !isMuted);

  if (isMuted) {
    muteIcon.innerText = '🔇';
    muteText.innerText = 'Unmute';
    micMuteBtn.style.backgroundColor = 'rgba(255, 51, 75, 0.2)';
  } else {
    muteIcon.innerText = '🎤';
    muteText.innerText = 'Mute';
    micMuteBtn.style.backgroundColor = '';
  }
});


// ------------------------------------------------------------------ Screen Keep-Alive (NoSleep + WakeLock)
function enableScreenKeepAlive() {
  if (window.NoSleep && !noSleep) {
    try {
      noSleep = new window.NoSleep();
      noSleep.enable();
      console.log('NoSleep.js keep-alive enabled.');
    } catch (e) {
      console.warn('NoSleep.js failed to enable:', e);
    }
  }

  if ('wakeLock' in navigator && !wakeLock) {
    navigator.wakeLock.request('screen').then(wl => {
      wakeLock = wl;
      wakeLock.addEventListener('release', () => {
        wakeLock = null;
      });
      console.log('Native Screen WakeLock active.');
    }).catch(err => {
      console.warn('Native Screen WakeLock request failed:', err);
    });
  }
}

function disableScreenKeepAlive() {
  if (wakeLock) {
    wakeLock.release().catch(() => {});
    wakeLock = null;
  }
  if (noSleep) {
    try {
      noSleep.disable();
    } catch (e) {}
    noSleep = null;
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && isTransmitting) {
    enableScreenKeepAlive();
  }
});


// ------------------------------------------------------------------ Audio Meters
function setupAudioContext() {
  if (!localMediaStream) return;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  audioContext = new AudioContextClass();

  const localSource = audioContext.createMediaStreamSource(localMediaStream);
  localMeterNode = audioContext.createAnalyser();
  localMeterNode.fftSize = 256;
  localSource.connect(localMeterNode);

  updateAudioMeters();
}

function hookRemoteAudioMeter(stream) {
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!audioContext) {
      audioContext = new AudioContextClass();
    }
    const remoteSource = audioContext.createMediaStreamSource(stream);
    remoteMeterNode = audioContext.createAnalyser();
    remoteMeterNode.fftSize = 256;
    remoteSource.connect(remoteMeterNode);
    if (!meterAnimationId) {
      updateAudioMeters();
    }
  } catch (e) {
    console.warn('Could not hook remote audio meter:', e);
  }
}

function updateAudioMeters() {
  // Read local peak
  if (localMeterNode && isTransmitting && !isMuted) {
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


// Start checking auth status on page load
checkAuthStatus();
