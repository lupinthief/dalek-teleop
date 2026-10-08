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

// Virtual Joystick State
let joystickVisible = false;
let joystickActive = false;
let joystickTouchId = null;
let currentRx = 0.0;
let currentRy = 0.0;
let joystickSendInterval = null;

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
const stickToggleBtn = document.getElementById('stick-toggle-btn');
const stickToggleIcon = document.getElementById('stick-toggle-icon');
const stickToggleText = document.getElementById('stick-toggle-text');
const micMuteBtn = document.getElementById('mic-mute-btn');
const muteIcon = document.getElementById('mute-icon');
const muteText = document.getElementById('mute-text');
const logoutBtn = document.getElementById('logout-btn');

const joystickContainer = document.getElementById('joystick-container');
const joystickBase = document.getElementById('joystick-base');
const joystickStick = document.getElementById('joystick-stick');
const joystickReadout = document.getElementById('joystick-readout');

const outboundMeterFill = document.getElementById('outbound-meter-fill');
const inboundMeterFill = document.getElementById('inbound-meter-fill');
const remoteAudio = document.getElementById('remote-audio');

const volumeSlider = document.getElementById('volume-slider');
const volumeValue = document.getElementById('volume-value');
const volumeIcon = document.getElementById('volume-icon');


// ------------------------------------------------------------------ Init & Auth Flow
async function checkAuthStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    if (data.authenticated) {
      if (typeof data.volume === 'number') {
        updateVolumeUI(data.volume, data.muted);
      }
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
      } else if (msg.action === 'volume_update') {
        updateVolumeUI(msg.volume, msg.muted);
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
  if (localMediaStream) {
    localMediaStream.getTracks().forEach(t => t.stop());
    localMediaStream = null;
  }
  if (meterAnimationId) {
    cancelAnimationFrame(meterAnimationId);
    meterAnimationId = null;
  }
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


// Unlock remote audio on any user interaction with the dashboard
function tryUnlockRemoteAudio() {
  if (remoteAudio && remoteAudio.srcObject && remoteAudio.paused) {
    remoteAudio.play().then(() => {
      console.log("Remote Dalek room audio successfully unmuted/playing");
    }).catch(e => {
      console.log("Audio unlock attempt deferred:", e);
    });
  }
}

document.addEventListener("pointerdown", tryUnlockRemoteAudio, { passive: true });
document.addEventListener("touchstart", tryUnlockRemoteAudio, { passive: true });
document.addEventListener("click", tryUnlockRemoteAudio, { passive: true });

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
    if (!localMediaStream) {
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
    }

    const audioTrack = localMediaStream.getAudioTracks()[0];
    if (audioTrack) {
      audioTrack.enabled = true;
    }

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
    isMuted = false;
    muteIcon.innerText = '🎤';
    muteText.innerText = 'Mute';
    micMuteBtn.style.backgroundColor = '';
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

  // Instead of tearing down the track or replacing with null (which breaks
  // the peer connection's remote receive loop on aiortc without renegotiation),
  // mute the track so RTP continues streaming silence smoothly.
  if (localMediaStream) {
    localMediaStream.getAudioTracks().forEach(t => t.enabled = false);
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


// ------------------------------------------------------------------ Virtual Joystick
function sendJoystickCommand(rx, ry) {
  if (signalingSocket && signalingSocket.readyState === WebSocket.OPEN) {
    try {
      signalingSocket.send(JSON.stringify({
        action: 'joystick',
        rx: Math.round(rx * 1000) / 1000,
        ry: Math.round(ry * 1000) / 1000
      }));
    } catch (e) {
      console.warn('Failed to send joystick command:', e);
    }
  }
}

function startJoystickLoop() {
  if (!joystickSendInterval) {
    joystickSendInterval = setInterval(() => {
      if (joystickActive || currentRx !== 0 || currentRy !== 0) {
        sendJoystickCommand(currentRx, currentRy);
      }
    }, 50); // 20 Hz
  }
}

function stopJoystickLoop() {
  if (joystickSendInterval) {
    clearInterval(joystickSendInterval);
    joystickSendInterval = null;
  }
  sendJoystickCommand(0, 0);
}

function toggleJoystick(forcedState) {
  joystickVisible = forcedState !== undefined ? forcedState : !joystickVisible;
  if (joystickVisible) {
    joystickContainer.classList.remove('hidden');
    stickToggleBtn.classList.add('active');
    startJoystickLoop();
  } else {
    resetJoystick();
    stopJoystickLoop();
    joystickContainer.classList.add('hidden');
    stickToggleBtn.classList.remove('active');
  }
}

stickToggleBtn.addEventListener('click', () => {
  toggleJoystick();
});

function updateStickPosition(clientX, clientY) {
  const rect = joystickBase.getBoundingClientRect();
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  const maxRadius = (rect.width / 2) - 10; // keep knob inside rim

  let dx = clientX - centerX;
  let dy = clientY - centerY;
  const dist = Math.hypot(dx, dy);

  if (dist > maxRadius) {
    dx = (dx / dist) * maxRadius;
    dy = (dy / dist) * maxRadius;
  }

  joystickStick.style.transform = `translate(${dx}px, ${dy}px)`;

  // Normalized values: rx in [-1, 1], ry in [-1, 1]
  // On gamepads: push up is ry > 0, push right is rx > 0
  // In DOM: moving up is dy < 0, so ry = -dy / maxRadius
  let normRx = dx / maxRadius;
  let normRy = -dy / maxRadius;

  // Small deadzone (0.05)
  if (Math.abs(normRx) < 0.05) normRx = 0.0;
  if (Math.abs(normRy) < 0.05) normRy = 0.0;

  currentRx = Math.max(-1.0, Math.min(1.0, normRx));
  currentRy = Math.max(-1.0, Math.min(1.0, normRy));

  joystickReadout.innerText = `HEAD: ${currentRx.toFixed(2)} | EYE: ${currentRy.toFixed(2)}`;
}

function resetJoystick() {
  joystickActive = false;
  joystickTouchId = null;
  currentRx = 0.0;
  currentRy = 0.0;
  joystickStick.style.transform = 'translate(0px, 0px)';
  joystickReadout.innerText = 'HEAD: 0.00 | EYE: 0.00';
  sendJoystickCommand(0.0, 0.0);
}

// Touch events for mobile
joystickBase.addEventListener('touchstart', (e) => {
  e.preventDefault();
  if (e.targetTouches.length > 0) {
    const touch = e.targetTouches[0];
    joystickTouchId = touch.identifier;
    joystickActive = true;
    updateStickPosition(touch.clientX, touch.clientY);
  }
}, { passive: false });

window.addEventListener('touchmove', (e) => {
  if (!joystickActive) return;
  for (let i = 0; i < e.changedTouches.length; i++) {
    const touch = e.changedTouches[i];
    if (touch.identifier === joystickTouchId) {
      updateStickPosition(touch.clientX, touch.clientY);
      break;
    }
  }
}, { passive: false });

window.addEventListener('touchend', (e) => {
  if (!joystickActive) return;
  for (let i = 0; i < e.changedTouches.length; i++) {
    if (e.changedTouches[i].identifier === joystickTouchId) {
      resetJoystick();
      break;
    }
  }
});

window.addEventListener('touchcancel', (e) => {
  if (joystickActive) {
    resetJoystick();
  }
});

// Mouse events for desktop testing
joystickBase.addEventListener('mousedown', (e) => {
  joystickActive = true;
  updateStickPosition(e.clientX, e.clientY);
});

window.addEventListener('mousemove', (e) => {
  if (joystickActive && joystickTouchId === null) {
    updateStickPosition(e.clientX, e.clientY);
  }
});

window.addEventListener('mouseup', () => {
  if (joystickActive && joystickTouchId === null) {
    resetJoystick();
  }
});


// ------------------------------------------------------------------ Volume Control
function updateVolumeUI(volume, muted) {
  if (volumeSlider) {
    volumeSlider.value = volume;
  }
  if (volumeValue) {
    volumeValue.innerText = muted ? 'MUTED' : `${volume}%`;
  }
  if (volumeIcon) {
    volumeIcon.innerText = muted || volume === 0 ? '🔇' : (volume < 50 ? '🔉' : '🔊');
  }
}

let volumeDebounce = null;
function sendVolumeCommand(vol, mute = null) {
  if (volumeDebounce) clearTimeout(volumeDebounce);
  volumeDebounce = setTimeout(() => {
    if (signalingSocket && signalingSocket.readyState === WebSocket.OPEN) {
      signalingSocket.send(JSON.stringify({
        action: 'volume',
        volume: vol,
        muted: mute
      }));
    } else {
      // Fallback to REST API
      fetch('/api/volume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ volume: vol, muted: mute })
      })
      .then(res => res.json())
      .then(data => updateVolumeUI(data.volume, data.muted))
      .catch(err => console.debug('Failed to set volume via API:', err));
    }
  }, 50);
}

if (volumeSlider) {
  volumeSlider.addEventListener('input', (e) => {
    const val = parseInt(e.target.value, 10);
    updateVolumeUI(val, false);
    sendVolumeCommand(val, false);
  });
}

if (volumeIcon) {
  volumeIcon.addEventListener('click', () => {
    const isCurrentlyMuted = volumeValue.innerText === 'MUTED';
    sendVolumeCommand(parseInt(volumeSlider.value, 10), !isCurrentlyMuted);
  });
}
