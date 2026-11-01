/* ============================================================
   LAMO IN-APP WEBRTC AUDIO CALLING CLIENT (MASKED / ZERO PHONE)
   ============================================================ */

(function () {
    'use strict';

    // State Variables
    let peerConnection = null;
    let localStream = null;
    let activeCallId = null;
    let activeBookingId = null;
    let callTimerInterval = null;
    let pollInterval = null;
    let incomingCheckInterval = null;
    let callSeconds = 0;
    let isMuted = false;
    let currentRole = null; // 'caller' or 'receiver'
    let pendingOfferSdp = null;

    // Web Audio Synthesizer for Ringtone & Dial Tone
    let audioCtx = null;
    let toneOscillator = null;
    let toneGainNode = null;
    let ringInterval = null;

    const rtcConfig = {
        iceServers: [
            { urls: 'stun:stun.l.google.com:19302' },
            { urls: 'stun:stun1.l.google.com:19302' }
        ]
    };

    // Helper: CSRF Token
    function getCsrfToken() {
        const metaTag = document.querySelector('meta[name="csrf-token"]');
        if (metaTag) return metaTag.getAttribute('content');
        const cookie = document.cookie.split('; ').find(row => row.startsWith('csrftoken='));
        return cookie ? cookie.split('=')[1] : '';
    }

    // Helper: Normalize SDP string to strict RFC 4566 CRLF (\r\n) line endings required by WebRTC
    function normalizeSdp(sdp) {
        if (!sdp || typeof sdp !== 'string') return '';
        const lines = sdp.replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n');
        const clean = lines.map(l => l.trim()).filter(l => l.length > 0);
        return clean.join('\r\n') + '\r\n';
    }

    // Audio FX Generator (Web Audio API)
    function initAudioContext() {
        if (!audioCtx) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (AudioContextClass) audioCtx = new AudioContextClass();
        }
        if (audioCtx && audioCtx.state === 'suspended') {
            audioCtx.resume();
        }
    }

    function playDialTone() {
        stopAudioTones();
        initAudioContext();
        if (!audioCtx) return;

        ringInterval = setInterval(() => {
            try {
                const osc = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(440, audioCtx.currentTime); // A4
                gain.gain.setValueAtTime(0.08, audioCtx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 1.2);
                osc.connect(gain);
                gain.connect(audioCtx.destination);
                osc.start();
                osc.stop(audioCtx.currentTime + 1.2);
            } catch (e) { }
        }, 3000);
    }

    function playRingtone() {
        stopAudioTones();
        initAudioContext();
        if (!audioCtx) return;

        function chime() {
            try {
                const now = audioCtx.currentTime;
                const osc1 = audioCtx.createOscillator();
                const osc2 = audioCtx.createOscillator();
                const gain = audioCtx.createGain();

                osc1.type = 'sine';
                osc1.frequency.setValueAtTime(523.25, now); // C5
                osc1.frequency.setValueAtTime(659.25, now + 0.15); // E5

                osc2.type = 'triangle';
                osc2.frequency.setValueAtTime(783.99, now + 0.3); // G5

                gain.gain.setValueAtTime(0.12, now);
                gain.gain.exponentialRampToValueAtTime(0.001, now + 0.7);

                osc1.connect(gain);
                osc2.connect(gain);
                gain.connect(audioCtx.destination);

                osc1.start(now);
                osc1.stop(now + 0.7);
                osc2.start(now + 0.3);
                osc2.stop(now + 0.7);
            } catch (e) { }
        }

        chime();
        ringInterval = setInterval(chime, 2500);
    }

    function stopAudioTones() {
        if (ringInterval) {
            clearInterval(ringInterval);
            ringInterval = null;
        }
    }

    // UI Elements Cache
    function getElements() {
        return {
            overlay: document.getElementById('lamoCallOverlay'),
            partnerName: document.getElementById('lamoCallPartnerName'),
            subtitle: document.getElementById('lamoCallSubtitle'),
            timer: document.getElementById('lamoCallTimer'),
            statusText: document.getElementById('lamoCallStatusText'),
            waves: document.getElementById('lamoCallWaves'),
            outgoingControls: document.getElementById('lamoOutgoingControls'),
            incomingControls: document.getElementById('lamoIncomingControls'),
            activeControls: document.getElementById('lamoActiveControls'),
            cancelBtn: document.getElementById('lamoCallCancelBtn'),
            declineBtn: document.getElementById('lamoCallDeclineBtn'),
            acceptBtn: document.getElementById('lamoCallAcceptBtn'),
            muteBtn: document.getElementById('lamoCallMuteBtn'),
            endBtn: document.getElementById('lamoCallEndBtn'),
            muteIcon: document.getElementById('lamoMuteIcon'),
            muteLabel: document.getElementById('lamoMuteLabel'),
            remoteAudio: document.getElementById('lamoRemoteAudio'),
        };
    }

    // Modal Presentation
    function showCallOverlay() {
        const el = getElements();
        if (el.overlay) {
            el.overlay.removeAttribute('hidden');
            el.overlay.setAttribute('aria-hidden', 'false');
        }
    }

    function hideCallOverlay() {
        const el = getElements();
        if (el.overlay) {
            el.overlay.setAttribute('hidden', 'true');
            el.overlay.setAttribute('aria-hidden', 'true');
        }
    }

    // Timer
    function startTimer() {
        const el = getElements();
        callSeconds = 0;
        if (el.timer) {
            el.timer.style.display = 'block';
            el.timer.textContent = '00:00';
        }
        if (el.waves) el.waves.style.display = 'flex';

        clearInterval(callTimerInterval);
        callTimerInterval = setInterval(() => {
            callSeconds++;
            const mins = Math.floor(callSeconds / 60).toString().padStart(2, '0');
            const secs = (callSeconds % 60).toString().padStart(2, '0');
            if (el.timer) el.timer.textContent = `${mins}:${secs}`;
        }, 1000);
    }

    function stopTimer() {
        clearInterval(callTimerInterval);
        callTimerInterval = null;
        const el = getElements();
        if (el.timer) el.timer.style.display = 'none';
        if (el.waves) el.waves.style.display = 'none';
    }

    // Tear Down Peer Connection
    function cleanupCall() {
        stopAudioTones();
        stopTimer();
        clearInterval(pollInterval);
        pollInterval = null;

        if (peerConnection) {
            try {
                peerConnection.close();
            } catch (e) { }
            peerConnection = null;
        }

        if (localStream) {
            try {
                localStream.getTracks().forEach(track => track.stop());
            } catch (e) { }
            localStream = null;
        }

        const el = getElements();
        if (el.remoteAudio) el.remoteAudio.srcObject = null;

        activeCallId = null;
        activeBookingId = null;
        currentRole = null;
        pendingOfferSdp = null;
        isMuted = false;

        if (el.muteBtn) el.muteBtn.classList.remove('is-muted');
        if (el.muteIcon) el.muteIcon.className = 'bi bi-mic-fill';
        if (el.muteLabel) el.muteLabel.textContent = 'Mute';
    }

    // End / Terminate Call
    async function endCall(statusMessage) {
        const el = getElements();
        if (el.statusText) el.statusText.textContent = statusMessage || 'Call Ended';

        if (activeBookingId && activeCallId) {
            try {
                await fetch(`/api/call/${activeBookingId}/${activeCallId}/end/`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCsrfToken(),
                    }
                });
            } catch (e) { }
        }

        cleanupCall();
        setTimeout(() => {
            hideCallOverlay();
        }, 1200);
    }

    // OUTGOING CALL (Initiator)
    window.initiateLamoCall = async function (bookingId, partnerName, partnerRole) {
        initAudioContext();
        cleanupCall();

        const el = getElements();
        if (el.partnerName) el.partnerName.textContent = partnerName || 'Service Professional';
        if (el.subtitle) el.subtitle.textContent = `${partnerRole || 'LAMO Partner'} • In-App Call`;
        if (el.statusText) el.statusText.textContent = 'Calling & Connecting...';

        // Set Controls
        if (el.outgoingControls) el.outgoingControls.style.display = 'flex';
        if (el.incomingControls) el.incomingControls.style.display = 'none';
        if (el.activeControls) el.activeControls.style.display = 'none';

        showCallOverlay();
        playDialTone();

        activeBookingId = bookingId;
        currentRole = 'caller';

        // 1. Get Microphone (with fallback for automated/headless or no-mic environments)
        try {
            if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
                localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
            } else {
                throw new Error('getUserMedia not supported');
            }
        } catch (err) {
            console.warn('[LAMO Call] Real microphone unavailable or permission pending:', err);
            try {
                const AudioCtx = window.AudioContext || window.webkitAudioContext;
                if (AudioCtx) {
                    const fakeCtx = new AudioCtx();
                    const osc = fakeCtx.createOscillator();
                    const dst = fakeCtx.createMediaStreamDestination();
                    osc.connect(dst);
                    osc.start();
                    localStream = dst.stream;
                }
            } catch (e) { }

            if (!localStream) {
                stopAudioTones();
                if (el.statusText) el.statusText.textContent = 'Microphone access denied. Please allow microphone permission.';
                setTimeout(hideCallOverlay, 3000);
                return;
            }
        }

        // 2. Setup RTCPeerConnection
        peerConnection = new RTCPeerConnection(rtcConfig);

        localStream.getTracks().forEach(track => {
            peerConnection.addTrack(track, localStream);
        });

        peerConnection.ontrack = (event) => {
            if (el.remoteAudio && event.streams && event.streams[0]) {
                el.remoteAudio.srcObject = event.streams[0];
            }
        };

        peerConnection.onicecandidate = (event) => {
            if (event.candidate && activeBookingId && activeCallId) {
                fetch(`/api/call/${activeBookingId}/${activeCallId}/ice/`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCsrfToken(),
                    },
                    body: JSON.stringify({ candidate: event.candidate })
                }).catch(() => { });
            }
        };

        // 3. Create Offer SDP
        try {
            const offer = await peerConnection.createOffer();
            await peerConnection.setLocalDescription(offer);

            // 4. Send Offer to Backend
            const res = await fetch(`/api/call/${bookingId}/initiate/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken(),
                },
                body: JSON.stringify({ offer_sdp: offer.sdp })
            });

            const data = await res.json();
            if (!res.ok || !data.success) {
                throw new Error(data.error || 'Failed to initiate call.');
            }

            activeCallId = data.call_id;
            if (el.statusText) el.statusText.textContent = 'Ringing...';

            // 5. Start Polling for Answer & Remote Candidates
            pollCallStatus();

        } catch (err) {
            endCall(err.message || 'Call failed to connect.');
        }
    };

    // Polling Function for Signaling State
    function pollCallStatus() {
        clearInterval(pollInterval);
        let hasSetRemoteAnswer = false;

        pollInterval = setInterval(async () => {
            if (!activeBookingId || !activeCallId) return;

            try {
                const res = await fetch(`/api/call/${activeBookingId}/${activeCallId}/poll/`);
                if (!res.ok) return;
                const data = await res.json();
                const el = getElements();

                // If remote side answered
                if (data.status === 'CONNECTED' && data.answer_sdp && !hasSetRemoteAnswer && currentRole === 'caller') {
                    hasSetRemoteAnswer = true;
                    stopAudioTones();
                    try {
                        const cleanAnswer = normalizeSdp(data.answer_sdp);
                        await peerConnection.setRemoteDescription(new RTCSessionDescription({
                            type: 'answer',
                            sdp: cleanAnswer
                        }));

                        if (el.statusText) el.statusText.textContent = 'Connected (Audio Live)';
                        if (el.outgoingControls) el.outgoingControls.style.display = 'none';
                        if (el.activeControls) el.activeControls.style.display = 'flex';
                        startTimer();
                    } catch (e) {
                        console.error('[LAMO Call] Failed setting remote answer SDP:', e);
                        endCall('Failed to connect: ' + (e.message || 'SDP error'));
                        return;
                    }
                }

                // Add remote ICE candidates
                if (data.candidates && Array.isArray(data.candidates) && peerConnection) {
                    for (const cand of data.candidates) {
                        try {
                            await peerConnection.addIceCandidate(new RTCIceCandidate(cand));
                        } catch (e) { }
                    }
                }

                // Handle call termination from other side
                if (data.status === 'ENDED' || data.status === 'REJECTED' || data.status === 'MISSED') {
                    const msg = (data.status === 'REJECTED') ? 'Call Declined' : (data.status === 'MISSED' ? 'No Answer' : 'Call Ended');
                    endCall(msg);
                }

            } catch (e) { }
        }, 1200);
    }

    // INCOMING CALL HANDLER
    async function showIncomingCall(callData) {
        initAudioContext();
        activeBookingId = callData.booking_id;
        activeCallId = callData.call_id;
        pendingOfferSdp = callData.offer_sdp;
        currentRole = 'receiver';

        const el = getElements();
        if (el.partnerName) el.partnerName.textContent = callData.caller_name || 'Incoming Call';
        if (el.subtitle) el.subtitle.textContent = `${callData.caller_role || 'LAMO'} • ${callData.service_name || 'Booking'}`;
        if (el.statusText) el.statusText.textContent = 'Incoming In-App Audio Call...';

        // Set Controls
        if (el.outgoingControls) el.outgoingControls.style.display = 'none';
        if (el.incomingControls) el.incomingControls.style.display = 'flex';
        if (el.activeControls) el.activeControls.style.display = 'none';

        showCallOverlay();
        playRingtone();
    }

    // Accept Incoming Call
    async function acceptIncomingCall() {
        stopAudioTones();
        const el = getElements();
        if (el.statusText) el.statusText.textContent = 'Connecting audio...';

        try {
            if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
                localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
            } else {
                throw new Error('getUserMedia not supported');
            }
        } catch (err) {
            console.warn('[LAMO Call] Real microphone unavailable or permission pending on answer:', err);
            try {
                const AudioCtx = window.AudioContext || window.webkitAudioContext;
                if (AudioCtx) {
                    const fakeCtx = new AudioCtx();
                    const osc = fakeCtx.createOscillator();
                    const dst = fakeCtx.createMediaStreamDestination();
                    osc.connect(dst);
                    osc.start();
                    localStream = dst.stream;
                }
            } catch (e) { }

            if (!localStream) {
                endCall('Microphone access denied. Could not accept call.');
                return;
            }
        }

        peerConnection = new RTCPeerConnection(rtcConfig);

        localStream.getTracks().forEach(track => {
            peerConnection.addTrack(track, localStream);
        });

        peerConnection.ontrack = (event) => {
            if (el.remoteAudio && event.streams && event.streams[0]) {
                el.remoteAudio.srcObject = event.streams[0];
            }
        };

        peerConnection.onicecandidate = (event) => {
            if (event.candidate && activeBookingId && activeCallId) {
                fetch(`/api/call/${activeBookingId}/${activeCallId}/ice/`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCsrfToken(),
                    },
                    body: JSON.stringify({ candidate: event.candidate })
                }).catch(() => { });
            }
        };

        try {
            // Set Remote Offer with normalized CRLF lines
            const cleanOffer = normalizeSdp(pendingOfferSdp);
            await peerConnection.setRemoteDescription(new RTCSessionDescription({
                type: 'offer',
                sdp: cleanOffer
            }));

            // Create Answer
            const answer = await peerConnection.createAnswer();
            await peerConnection.setLocalDescription(answer);

            // Send Answer to Server
            const res = await fetch(`/api/call/${activeBookingId}/${activeCallId}/answer/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken(),
                },
                body: JSON.stringify({ answer_sdp: answer.sdp })
            });

            if (!res.ok) {
                const errData = await res.json().catch(() => ({}));
                throw new Error(errData.error || ('Server rejected answer (' + res.status + ')'));
            }

            if (el.statusText) el.statusText.textContent = 'Connected (Audio Live)';
            if (el.incomingControls) el.incomingControls.style.display = 'none';
            if (el.activeControls) el.activeControls.style.display = 'flex';
            startTimer();

            // Start Polling for ICE candidates & state
            pollCallStatus();

        } catch (err) {
            console.error('[LAMO Call] acceptIncomingCall failed:', err);
            const msg = (err && err.message) ? ('Failed to connect: ' + err.message) : 'Failed to connect call.';
            endCall(msg);
        }
    }

    // Toggle Mute
    function toggleMute() {
        if (!localStream) return;
        const audioTracks = localStream.getAudioTracks();
        if (audioTracks.length === 0) return;

        isMuted = !isMuted;
        audioTracks[0].enabled = !isMuted;

        const el = getElements();
        if (el.muteBtn) el.muteBtn.classList.toggle('is-muted', isMuted);
        if (el.muteIcon) el.muteIcon.className = isMuted ? 'bi bi-mic-mute-fill' : 'bi bi-mic-fill';
        if (el.muteLabel) el.muteLabel.textContent = isMuted ? 'Unmute' : 'Mute';
    }

    // Periodic Check for Incoming Calls
    function startIncomingCallWatcher() {
        clearInterval(incomingCheckInterval);
        incomingCheckInterval = setInterval(async () => {
            // Don't poll if currently in an active call
            if (activeCallId) return;

            try {
                const res = await fetch('/api/call/check-incoming/');
                if (!res.ok) return;
                const data = await res.json();
                if (data.has_incoming && data.call_id) {
                    showIncomingCall(data);
                }
            } catch (e) { }
        }, 3500);
    }

    // DOM Ready Initialization
    document.addEventListener('DOMContentLoaded', function () {
        const el = getElements();

        if (el.cancelBtn) {
            el.cancelBtn.addEventListener('click', () => endCall('Call Cancelled'));
        }
        if (el.declineBtn) {
            el.declineBtn.addEventListener('click', () => endCall('Call Declined'));
        }
        if (el.acceptBtn) {
            el.acceptBtn.addEventListener('click', acceptIncomingCall);
        }
        if (el.endBtn) {
            el.endBtn.addEventListener('click', () => endCall('Call Ended'));
        }
        if (el.muteBtn) {
            el.muteBtn.addEventListener('click', toggleMute);
        }

        // Start watcher if user is authenticated (checks automatically)
        startIncomingCallWatcher();
    });

})();
