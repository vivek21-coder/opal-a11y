import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { t } from './i18n';

const STATUS = {
  CONNECTING: 'connecting',
  CONNECTED: 'connected',
  DISCONNECTED: 'disconnected',
};

// Examples are now provided by the i18n file per language

const LANGUAGES = [
  { code: 'en', label: 'English', name: 'English' },
  { code: 'es', label: 'Español', name: 'Spanish' },
  { code: 'fr', label: 'Français', name: 'French' },
  { code: 'de', label: 'Deutsch', name: 'German' },
  { code: 'zh', label: '中文', name: 'Chinese' },
  { code: 'hi', label: 'हिन्दी', name: 'Hindi' },
  { code: 'ar', label: 'العربية', name: 'Arabic' },
  { code: 'pt', label: 'Português', name: 'Portuguese' },
  { code: 'ja', label: '日本語', name: 'Japanese' },
  { code: 'ko', label: '한국어', name: 'Korean' },
];

let backendUrl = 'ws://localhost:8080';
if (window.BACKEND_URL && /^https?:\/\/.+/.test(window.BACKEND_URL)) {
  backendUrl = window.BACKEND_URL;
}

const WS_URL = backendUrl.replace(/^http/, 'ws') + '/ws/agent';

// Silence detection config
const SILENCE_THRESHOLD = 0.015; // RMS threshold for silence (below = silence)
const SPEECH_THRESHOLD = 0.04;   // RMS threshold for speech (above = real speech)
const SILENCE_DURATION_MS = 2500; // ms of silence before auto-stop
const MIN_WORDS_FOR_AUTO_STOP = 2; // don't auto-stop if fewer words detected
const MIN_SPEECH_FRAMES = 10;     // minimum frames above SPEECH_THRESHOLD to count as speech

let msgCounter = 0;
function nextMsgId() {
  return `msg_${Date.now()}_${++msgCounter}`;
}

export default function App() {
  const [task, setTask] = useState('');
  const [messages, setMessages] = useState([]);

  const [isListening, setIsListening] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [statusMsg, setStatusMsg] = useState('');
  const [connStatus, setConnStatus] = useState(STATUS.CONNECTING);

  const [ttsEnabled, setTtsEnabled] = useState(true);
  const [isSpeaking, setIsSpeaking] = useState(false);

  const [activeUrl, setActiveUrl] = useState(null);
  // { url, screenshot }

  const [language, setLanguage] = useState('English');
  const [showLanguagePicker, setShowLanguagePicker] = useState(false);
  const [showInfo, setShowInfo] = useState(false);

  // Theme state
  const [theme, setTheme] = useState(() => localStorage.getItem('opal-theme') || 'system');
  const [showThemePicker, setShowThemePicker] = useState(false);
  const [focusedThemeIndex, setFocusedThemeIndex] = useState(-1);

  // Localised UI strings — updates whenever language changes
  const strings = useMemo(() => t(language), [language]);

  // Update browser tab title when language changes
  useEffect(() => {
    document.title = strings.appTitle;
  }, [strings]);

  const [focusedLangIndex, setFocusedLangIndex] = useState(-1);

  const socket = useRef(null);
  const reconnectTimer = useRef(null);
  const chatScrollRef = useRef(null);
  const inputRef = useRef(null);
  const viewportRef = useRef(null);

  // Audio recording refs
  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const audioContextRef = useRef(null);
  const analyserRef = useRef(null);
  const silenceTimerRef = useRef(null);
  const silenceCheckRef = useRef(null);
  const mediaStreamRef = useRef(null);

  // TTS playback refs
  const audioQueueRef = useRef([]);
  const currentAudioRef = useRef(null);
  const isPlayingRef = useRef(false);
  const handleTtsChunkRef = useRef(null);

  // Track which message is currently streaming
  const currentStreamIdRef = useRef(null);

  // Auto-submit after STT
  const autoSubmitRef = useRef(false);

  // Track if enough speech has been detected for auto-stop
  const speechDetectedRef = useRef(false);
  const speechFrameCountRef = useRef(0);

  // Voice-loop: auto-restart STT after TTS finishes
  const voiceSessionRef = useRef(false);   // true while in voice conversation loop
  const ttsAllReceivedRef = useRef(false);  // true once TTS_DONE arrives

  // Language dropdown ref
  const langDropdownRef = useRef(null);
  const langButtonRef = useRef(null);

  // Theme dropdown refs
  const themeDropdownRef = useRef(null);
  const themeButtonRef = useRef(null);
  const creditsScrollRef = useRef(null);
  const [creditsFade, setCreditsFade] = useState('fade-right');

  const handleCreditsScroll = useCallback(() => {
    const el = creditsScrollRef.current;
    if (!el) return;
    const atStart = el.scrollLeft < 5;
    const atEnd = el.scrollLeft + el.clientWidth >= el.scrollWidth - 5;
    if (atStart && atEnd) setCreditsFade('');
    else if (atStart) setCreditsFade('fade-right');
    else if (atEnd) setCreditsFade('fade-left');
    else setCreditsFade('fade-both');
  }, []);

  // Keep a ref to the current language so callbacks always see the latest value
  const languageRef = useRef(language);
  useEffect(() => { languageRef.current = language; }, [language]);

  // --- Theme application ---
  useEffect(() => {
    const applyTheme = (resolved) => {
      document.documentElement.setAttribute('data-theme', resolved);
      const meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.content = resolved === 'light' ? '#f8f9fc' : '#080c14';
    };

    localStorage.setItem('opal-theme', theme);

    if (theme === 'system') {
      const mq = window.matchMedia('(prefers-color-scheme: light)');
      applyTheme(mq.matches ? 'light' : 'dark');
      const handler = (e) => applyTheme(e.matches ? 'light' : 'dark');
      mq.addEventListener('change', handler);
      return () => mq.removeEventListener('change', handler);
    } else {
      applyTheme(theme);
    }
  }, [theme]);

  // --- Theme dropdown: close on outside click + Escape ---
  useEffect(() => {
    if (!showThemePicker) return;
    const handleClickOutside = (e) => {
      if (themeDropdownRef.current && !themeDropdownRef.current.contains(e.target) &&
          themeButtonRef.current && !themeButtonRef.current.contains(e.target)) {
        setShowThemePicker(false);
      }
    };
    const handleEscape = (e) => {
      if (e.key === 'Escape') {
        setShowThemePicker(false);
        themeButtonRef.current?.focus();
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [showThemePicker]);

  // --- WebSocket ---
  const connectWebSocket = useCallback(() => {
    setConnStatus(STATUS.CONNECTING);
    const ws = new WebSocket(WS_URL);
    socket.current = ws;

    ws.onopen = () => {
      setConnStatus(STATUS.CONNECTED);
      // Always send the current language on (re)connect
      ws.send('SET_LANGUAGE:' + languageRef.current);
    };
    ws.onclose = () => {
      setConnStatus(STATUS.DISCONNECTED);
      // Reset running/streaming state so UI isn't stuck
      setIsRunning(false);
      setStatusMsg('');
      // Finalize any in-progress streaming message
      if (currentStreamIdRef.current) {
        const streamId = currentStreamIdRef.current;
        setMessages(prev => prev.map(m =>
          m.id === streamId ? { ...m, isStreaming: false } : m
        ));
      }
      currentStreamIdRef.current = null;
      // Stop any voice session since backend is gone
      voiceSessionRef.current = false;
      reconnectTimer.current = setTimeout(connectWebSocket, 3000);
    };
    ws.onerror = () => setConnStatus(STATUS.DISCONNECTED);

    ws.onmessage = (event) => {
      const data = event.data;

      if (data.startsWith('STATUS:')) {
        setStatusMsg(data.replace('STATUS:', '').trim());

      } else if (data.startsWith('SCREENSHOT:')) {
        try {
          const payload = JSON.parse(data.slice(11));
          setActiveUrl(payload);
        } catch (e) {
          console.warn('Failed to parse SCREENSHOT:', e);
        }

      } else if (data.startsWith('STREAM_START:')) {
        const newId = nextMsgId();
        currentStreamIdRef.current = newId;
        setMessages(prev => [...prev, {
          id: newId,
          role: 'assistant',
          text: '',
          isStreaming: true,
          timestamp: Date.now(),
        }]);
        setStatusMsg('');

      } else if (data.startsWith('STREAM_TOKEN:')) {
        const token = data.slice(14);
        const streamId = currentStreamIdRef.current;
        if (streamId) {
          setMessages(prev => prev.map(m =>
            m.id === streamId ? { ...m, text: m.text + token } : m
          ));
        }

      } else if (data.startsWith('STREAM_END:')) {
        const fullText = data.slice(12).trim();
        const streamId = currentStreamIdRef.current;
        if (streamId) {
          setMessages(prev => prev.map(m =>
            m.id === streamId ? { ...m, text: fullText, isStreaming: false } : m
          ));
        }
        currentStreamIdRef.current = null;
        setIsRunning(false);
        setStatusMsg('');

      } else if (data.startsWith('TTS_CHUNK:')) {
        try {
          const payload = JSON.parse(data.slice(10));
          // Use ref to always call latest handler (avoids stale closure)
          if (handleTtsChunkRef.current) handleTtsChunkRef.current(payload);
        } catch (e) {
          console.warn('Failed to parse TTS chunk:', e);
        }

      } else if (data.startsWith('TTS_DONE:')) {
        // All TTS chunks received — if nothing left to play, restart mic now
        ttsAllReceivedRef.current = true;
        voiceSessionRef.current = false;

      } else if (data.startsWith('STT_RESULT:')) {
        const transcript = data.slice(11).trim();
        if (transcript) {
          const wordCount = transcript.split(/\s+/).filter(w => w.length > 0).length;
          if (wordCount < MIN_WORDS_FOR_AUTO_STOP) {
            // Too few words — put text in input but don't submit
            setTask(transcript);
            setStatusMsg('');
            setIsListening(false);
          } else if (autoSubmitRef.current) {
            autoSubmitRef.current = false;
            submitText(transcript);
          } else {
            setTask(transcript);
          }
        }
        setStatusMsg('');
        setIsListening(false);

      } else if (data.startsWith('AGENT_RESULT:')) {
        const text = data.slice(13).trim();
        setMessages(prev => [...prev, {
          id: nextMsgId(),
          role: 'assistant',
          text,
          isStreaming: false,
          timestamp: Date.now(),
        }]);
        setIsRunning(false);
        setStatusMsg('');

      } else if (data.startsWith('CANCELLED:')) {
        // Backend acknowledged cancel — reset state
        setIsRunning(false);
        setStatusMsg('');
        if (currentStreamIdRef.current) {
          const streamId = currentStreamIdRef.current;
          setMessages(prev => prev.map(m =>
            m.id === streamId ? { ...m, isStreaming: false } : m
          ));
          currentStreamIdRef.current = null;
        }

      } else if (data.startsWith('ERROR:')) {
        const errorText = 'Error: ' + data.replace('ERROR:', '').trim();
        const streamId = currentStreamIdRef.current;
        if (streamId) {
          setMessages(prev => prev.map(m =>
            m.id === streamId ? { ...m, text: errorText, isStreaming: false } : m
          ));
          currentStreamIdRef.current = null;
        } else {
          setMessages(prev => [...prev, {
            id: nextMsgId(),
            role: 'assistant',
            text: errorText,
            isStreaming: false,
            timestamp: Date.now(),
          }]);
        }
        setIsRunning(false);
        setStatusMsg('');
      }
    };
  }, []);

  useEffect(() => {
    connectWebSocket();
    return () => {
      clearTimeout(reconnectTimer.current);
      if (socket.current) socket.current.close();
    };
  }, [connectWebSocket]);

  // Mic is off by default — user taps the mic button to start

  // Auto-scroll chat to bottom on any visible change
  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [messages, statusMsg]);

  // Send language to backend when it changes
  useEffect(() => {
    if (socket.current && socket.current.readyState === WebSocket.OPEN) {
      socket.current.send('SET_LANGUAGE:' + language);
    }
  }, [language]);

  // --- Language dropdown: close on outside click + Escape ---
  useEffect(() => {
    if (!showLanguagePicker) return;
    const handleClickOutside = (e) => {
      if (langDropdownRef.current && !langDropdownRef.current.contains(e.target) &&
          langButtonRef.current && !langButtonRef.current.contains(e.target)) {
        setShowLanguagePicker(false);
      }
    };
    const handleEscape = (e) => {
      if (e.key === 'Escape') {
        setShowLanguagePicker(false);
        langButtonRef.current?.focus();
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [showLanguagePicker]);

  // Keyboard navigation for language dropdown
  const handleLangKeyDown = (e) => {
    if (!showLanguagePicker) {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        setShowLanguagePicker(true);
        setFocusedLangIndex(0);
      }
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setFocusedLangIndex(i => Math.min(i + 1, LANGUAGES.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setFocusedLangIndex(i => Math.max(i - 1, 0));
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (focusedLangIndex >= 0) {
        setLanguage(LANGUAGES[focusedLangIndex].name);
        setShowLanguagePicker(false);
        langButtonRef.current?.focus();
      }
    }
  };

  // Auto-focus the active language option when dropdown opens
  useEffect(() => {
    if (showLanguagePicker && focusedLangIndex >= 0 && langDropdownRef.current) {
      const options = langDropdownRef.current.querySelectorAll('[role="option"]');
      if (options[focusedLangIndex]) options[focusedLangIndex].focus();
    }
  }, [showLanguagePicker, focusedLangIndex]);

  // Theme options
  const THEME_OPTIONS = useMemo(() => [
    { value: 'system', label: strings.theme_system },
    { value: 'dark', label: strings.theme_dark },
    { value: 'light', label: strings.theme_light },
  ], [strings]);

  // Keyboard navigation for theme dropdown
  const handleThemeKeyDown = (e) => {
    if (!showThemePicker) {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        setShowThemePicker(true);
        setFocusedThemeIndex(0);
      }
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setFocusedThemeIndex(i => Math.min(i + 1, THEME_OPTIONS.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setFocusedThemeIndex(i => Math.max(i - 1, 0));
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (focusedThemeIndex >= 0) {
        setTheme(THEME_OPTIONS[focusedThemeIndex].value);
        setShowThemePicker(false);
        themeButtonRef.current?.focus();
      }
    }
  };

  // Auto-focus theme option when dropdown opens
  useEffect(() => {
    if (showThemePicker && focusedThemeIndex >= 0 && themeDropdownRef.current) {
      const options = themeDropdownRef.current.querySelectorAll('[role="option"]');
      if (options[focusedThemeIndex]) options[focusedThemeIndex].focus();
    }
  }, [showThemePicker, focusedThemeIndex]);

  // --- TTS chunk playback (queued) ---
  const handleTtsChunk = useCallback((chunk) => {
    if (!ttsEnabled) return;
    audioQueueRef.current.push(chunk);
    playNextInQueue();
  }, [ttsEnabled]);

  // Keep ref in sync so WebSocket handler always uses latest version
  useEffect(() => {
    handleTtsChunkRef.current = handleTtsChunk;
  }, [handleTtsChunk]);

  const playNextInQueue = useCallback(() => {
    if (isPlayingRef.current) return;
    if (audioQueueRef.current.length === 0) {
      setIsSpeaking(false);
      voiceSessionRef.current = false;
      return;
    }

    isPlayingRef.current = true;
    setIsSpeaking(true);

    const chunk = audioQueueRef.current.shift();
    // Use Blob URL instead of data URL for more reliable playback
    try {
      const raw = atob(chunk.audio);
      const arr = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
      const blob = new Blob([arr], { type: chunk.mimeType || 'audio/mpeg' });
      const url = URL.createObjectURL(blob);
      const audioEl = new Audio(url);
      currentAudioRef.current = audioEl;

      const cleanup = () => {
        isPlayingRef.current = false;
        currentAudioRef.current = null;
        URL.revokeObjectURL(url);
        playNextInQueue();
      };

      audioEl.onended = cleanup;
      audioEl.onerror = cleanup;
      audioEl.play().catch(cleanup);
    } catch (e) {
      console.warn('TTS playback error:', e);
      isPlayingRef.current = false;
      currentAudioRef.current = null;
      playNextInQueue();
    }
  }, []);

  const stopSpeaking = () => {
    audioQueueRef.current = [];
    isPlayingRef.current = false;
    if (currentAudioRef.current) {
      currentAudioRef.current.pause();
      currentAudioRef.current.currentTime = 0;
      currentAudioRef.current = null;
    }
    setIsSpeaking(false);
  };

  // --- Cancel / stop current operation ---
  const cancelOperation = useCallback(() => {
    // Best-effort: send CANCEL to backend
    if (socket.current && socket.current.readyState === WebSocket.OPEN) {
      try { socket.current.send('CANCEL:'); } catch (_) { /* ignore */ }
    }

    // Stop TTS playback
    stopSpeaking();

    // Finalize any in-progress streaming message
    if (currentStreamIdRef.current) {
      const streamId = currentStreamIdRef.current;
      setMessages(prev => prev.map(m =>
        m.id === streamId ? { ...m, isStreaming: false } : m
      ));
      currentStreamIdRef.current = null;
    }

    // Reset running state
    setIsRunning(false);
    setStatusMsg('');
    voiceSessionRef.current = false;

    // Close WebSocket to kill in-flight backend work — auto-reconnect will fire
    if (socket.current) {
      socket.current.close();
    }
  }, []);

  // --- Real-time STT with silence detection ---
  const startListening = async () => {
    if (isListening) { stopListening(); return; }

    // Force stop TTS if it's speaking
    stopSpeaking();

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaStreamRef.current = stream;
      audioChunksRef.current = [];

      // Set up AudioContext for silence detection
      const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      audioContextRef.current = audioCtx;
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 2048;
      source.connect(analyser);
      analyserRef.current = analyser;

      // Set up MediaRecorder
      const mediaRecorder = new MediaRecorder(stream, {
        mimeType: MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
          ? 'audio/webm;codecs=opus'
          : 'audio/webm',
      });
      mediaRecorderRef.current = mediaRecorder;

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunksRef.current.push(e.data);
      };

      mediaRecorder.onstop = async () => {
        cleanupAudioResources();
        const blob = new Blob(audioChunksRef.current, { type: mediaRecorder.mimeType });
        // Skip if no real speech was detected or audio is too short
        if (!speechDetectedRef.current || blob.size < 5000) return;

        const wavBytes = await blobToWav(blob);
        const b64 = arrayBufferToBase64(wavBytes);

        if (socket.current && socket.current.readyState === WebSocket.OPEN) {
          socket.current.send('STT_AUDIO:' + JSON.stringify({ audio: b64, mimeType: 'audio/wav' }));
          setStatusMsg(strings.transcribing);
        }
      };

      mediaRecorder.start(250);
      setIsListening(true);
      autoSubmitRef.current = true; // Auto-submit after transcription
      voiceSessionRef.current = true;  // Mark voice session for auto-listen after TTS
      ttsAllReceivedRef.current = false;
      speechDetectedRef.current = false; // Reset speech tracking
      speechFrameCountRef.current = 0;

      // Start silence detection
      startSilenceDetection();

    } catch (err) {
      console.warn('Microphone access failed:', err);
      setMessages(prev => [...prev, {
        id: nextMsgId(), role: 'assistant',
        text: strings.micError,
        isStreaming: false, timestamp: Date.now(),
      }]);
    }
  };

  const startSilenceDetection = () => {
    if (!analyserRef.current) return;

    const analyser = analyserRef.current;
    const bufferLength = analyser.fftSize;
    const dataArray = new Float32Array(bufferLength);
    let silenceStart = null;

    const checkSilence = () => {
      if (!analyserRef.current) return;

      analyser.getFloatTimeDomainData(dataArray);

      // Calculate RMS
      let sum = 0;
      for (let i = 0; i < bufferLength; i++) {
        sum += dataArray[i] * dataArray[i];
      }
      const rms = Math.sqrt(sum / bufferLength);

      if (rms < SILENCE_THRESHOLD) {
        if (silenceStart === null) {
          silenceStart = Date.now();
        } else if (Date.now() - silenceStart > SILENCE_DURATION_MS) {
          // Only auto-stop if real speech was detected (not just noise)
          if (speechDetectedRef.current) {
            stopListening();
            return;
          }
        }
      } else {
        silenceStart = null; // Reset on any sound
        // Only count as speech if volume is above the speech threshold
        if (rms >= SPEECH_THRESHOLD) {
          speechFrameCountRef.current += 1;
          if (speechFrameCountRef.current >= MIN_SPEECH_FRAMES) {
            speechDetectedRef.current = true;
          }
        }
      }

      silenceCheckRef.current = requestAnimationFrame(checkSilence);
    };

    silenceCheckRef.current = requestAnimationFrame(checkSilence);
  };

  const cleanupAudioResources = () => {
    if (silenceCheckRef.current) {
      cancelAnimationFrame(silenceCheckRef.current);
      silenceCheckRef.current = null;
    }
    if (audioContextRef.current) {
      audioContextRef.current.close().catch(() => {});
      audioContextRef.current = null;
    }
    analyserRef.current = null;
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach(t => t.stop());
      mediaStreamRef.current = null;
    }
  };

  const stopListening = () => {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      mediaRecorderRef.current.stop();
    }
    mediaRecorderRef.current = null;
    setIsListening(false);
  };

  // --- Submit text (used by both manual send and auto-submit) ---
  const submitText = useCallback((text) => {
    const trimmed = text.trim();
    if (!trimmed || !socket.current || socket.current.readyState !== WebSocket.OPEN) return;

    setMessages(prev => [...prev, {
      id: nextMsgId(),
      role: 'user',
      text: trimmed,
      timestamp: Date.now(),
      url: isUrlInput(trimmed) ? trimmed : null,
    }]);

    setIsRunning(true);
    setTask('');
    setStatusMsg('');
    ttsAllReceivedRef.current = false; // Reset for upcoming TTS
    stopSpeaking();

    const msg = { text: trimmed };
    if (viewportRef.current) {
      msg.viewport = {
        width: viewportRef.current.clientWidth,
        height: viewportRef.current.clientHeight,
      };
    }
    socket.current.send('CHAT_MSG:' + JSON.stringify(msg));
  }, []);

  // --- Send message ---
  const sendMessage = () => {
    voiceSessionRef.current = false; // Typed message — no auto-listen
    submitText(task);
  };

  const newChat = () => {
    setMessages([]);
    setActiveUrl(null);
    setTask('');
    setStatusMsg('');
    stopSpeaking();
    if (socket.current && socket.current.readyState === WebSocket.OPEN) {
      socket.current.send('NEW_CHAT:');
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  };

  const hasMessages = messages.length > 0;

  return (
    <div className="app">
      <a href="#task-input" className="skip-link">{strings.skipToContent}</a>

      {/* Left panel: full viewport */}
      <main ref={viewportRef} className="panel-left" aria-label={strings.websiteViewport}>
        <h2 className="sr-only">{strings.websitePreview}</h2>
        {activeUrl && activeUrl.screenshot ? (
          <>
            <div className="viewport-toolbar">
              <div className="viewport-url-bar" title={activeUrl.url}>{activeUrl.url}</div>
              <a className="btn-icon btn-sm" href={activeUrl.url} target="_blank" rel="noopener noreferrer" aria-label={strings.openInNewTab} title={strings.openInNewTab}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
              </a>
              <button className="btn-icon btn-sm" onClick={() => setActiveUrl(null)} aria-label={strings.closePreview} title={strings.close}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
            <div className="viewport-screenshot">
              <img
                src={`data:image/png;base64,${activeUrl.screenshot}`}
                alt={`Screenshot of ${activeUrl.url}`}
                className="viewport-screenshot-img"
              />
            </div>
          </>
        ) : (
          <div className="viewport-empty">
            <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="0.8" strokeLinecap="round" strokeLinejoin="round" opacity="0.2">
              <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
            </svg>
            <p>{strings.viewportEmpty}</p>
            <span className="viewport-hint">{strings.viewportHint}</span>
          </div>
        )}
      </main>

      {/* Right panel: chat sidebar */}
      <aside className="panel-right" aria-label={strings.chat}>
        <header className="panel-header">
          <div className="title-group">
            <h1 className="app-title">{strings.appTitle}</h1>
            <button
              className="btn-info"
              onClick={() => setShowInfo(true)}
              aria-label={strings.infoButton}
              title={strings.infoButton}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
            </button>
          </div>
          <div className="header-actions">
            {/* Theme selector */}
            <div className="theme-picker-wrap">
              <button
                ref={themeButtonRef}
                className="btn-theme"
                onClick={() => {
                  setShowThemePicker(!showThemePicker);
                  if (!showThemePicker) {
                    const idx = THEME_OPTIONS.findIndex(o => o.value === theme);
                    setFocusedThemeIndex(idx >= 0 ? idx : 0);
                  }
                }}
                onKeyDown={handleThemeKeyDown}
                aria-label={`${strings.themeLabel}: ${THEME_OPTIONS.find(o => o.value === theme)?.label}`}
                aria-haspopup="listbox"
                aria-expanded={showThemePicker}
                title={`${strings.themeLabel}: ${THEME_OPTIONS.find(o => o.value === theme)?.label}`}
              >
                {theme === 'light' ? (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
                ) : theme === 'dark' ? (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
                ) : (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
                )}
              </button>
              {showThemePicker && (
                <div ref={themeDropdownRef} className="theme-dropdown" role="listbox" aria-label={strings.selectTheme}>
                  {THEME_OPTIONS.map((opt, idx) => (
                    <button
                      key={opt.value}
                      className={`theme-option ${theme === opt.value ? 'active' : ''} ${focusedThemeIndex === idx ? 'focused' : ''}`}
                      onClick={() => {
                        setTheme(opt.value);
                        setShowThemePicker(false);
                        themeButtonRef.current?.focus();
                      }}
                      onKeyDown={handleThemeKeyDown}
                      role="option"
                      aria-selected={theme === opt.value}
                      tabIndex={focusedThemeIndex === idx ? 0 : -1}
                    >
                      {opt.value === 'light' && (
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
                      )}
                      {opt.value === 'dark' && (
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
                      )}
                      {opt.value === 'system' && (
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
                      )}
                      {opt.label}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Language selector */}
            <div className="language-picker-wrap">
              <button
                ref={langButtonRef}
                className="btn-language"
                onClick={() => {
                  setShowLanguagePicker(!showLanguagePicker);
                  if (!showLanguagePicker) {
                    const idx = LANGUAGES.findIndex(l => l.name === language);
                    setFocusedLangIndex(idx >= 0 ? idx : 0);
                  }
                }}
                onKeyDown={handleLangKeyDown}
                aria-label={`Language: ${language}`}
                aria-haspopup="listbox"
                aria-expanded={showLanguagePicker}
                title={`Language: ${language}`}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
                {LANGUAGES.find(l => l.name === language)?.label || language}
              </button>
              {showLanguagePicker && (
                <div ref={langDropdownRef} className="language-dropdown" role="listbox" aria-label={strings.selectLanguage}>
                  {LANGUAGES.map((lang, idx) => (
                    <button
                      key={lang.code}
                      className={`language-option ${language === lang.name ? 'active' : ''} ${focusedLangIndex === idx ? 'focused' : ''}`}
                      onClick={() => {
                        setLanguage(lang.name);
                        setShowLanguagePicker(false);
                        langButtonRef.current?.focus();
                      }}
                      onKeyDown={handleLangKeyDown}
                      role="option"
                      aria-selected={language === lang.name}
                      tabIndex={focusedLangIndex === idx ? 0 : -1}
                    >
                      <span className="lang-label">{lang.label}</span>
                      <span className="lang-name">{lang.name}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>

            {hasMessages && (
              <button className="btn-new-chat" onClick={newChat} aria-label={strings.newConversation}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                {strings.newChat}
              </button>
            )}
          </div>
        </header>

        {/* Chat Messages */}
        <h2 className="sr-only">{strings.conversation}</h2>
        <div
          ref={chatScrollRef}
          className="chat-messages"
          role="log"
          aria-live="polite"
          aria-label={strings.chatHistory}
        >
          {messages.length === 0 && !statusMsg && (
            <div className="empty-state">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" opacity="0.3"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
              <p>{strings.emptyState}</p>
              <p className="empty-voice-hint">{strings.emptyVoiceHint}</p>

              <div className="examples-group">
                <span className="section-label">{strings.tryExample}</span>
                <div className="examples">
                  {strings.examples.map((prompt) => (
                    <button
                      key={prompt}
                      className="btn-example"
                      onClick={() => { setTask(prompt); inputRef.current?.focus(); }}
                      aria-label={prompt}
                    >
                      {prompt}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {messages.map(msg => (
            <div
              key={msg.id}
              className={`chat-bubble chat-${msg.role}`}
            >
              <div className="bubble-text">
                {msg.isStreaming ? (
                  <>{renderPlainText(msg.text)}<span className="cursor" aria-hidden="true" /></>
                ) : (
                  renderPlainText(msg.text)
                )}
              </div>
              {msg.url && (
                <div className="bubble-url">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
                  {msg.url}
                </div>
              )}
            </div>
          ))}

          {/* Loading indicator */}
          {isRunning && statusMsg && !messages.some(m => m.isStreaming) && (
            <div className="chat-bubble chat-assistant">
              <div className="bubble-text">
                <p className="status-msg">
                  <span className="spinner" aria-hidden="true" />
                  {statusMsg}
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Chat input area */}
        <div className="chat-input-area">
          <label htmlFor="task-input" className="sr-only">{strings.inputPlaceholder}</label>
          <textarea
            ref={inputRef}
            id="task-input"
            className="input"
            rows={2}
            value={task}
            onChange={(e) => setTask(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={strings.inputPlaceholder}
          />
          <div className="input-actions">
            {isRunning ? (
              <button
                className="btn-send btn-stop"
                onClick={cancelOperation}
                aria-label={strings.stopTask}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
                {strings.stop}
              </button>
            ) : (
              <button
                className="btn-send"
                onClick={sendMessage}
                disabled={!task.trim() || connStatus !== STATUS.CONNECTED}
                aria-label={strings.send}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
                {strings.send}
              </button>
            )}

            <button
              className={`btn-icon ${isListening ? 'btn-listening' : ''}`}
              onClick={isListening ? stopListening : startListening}
              disabled={isRunning && !isListening}
              aria-label={isListening ? strings.stopRecording : strings.voiceInput}
              title={isListening ? strings.listening : strings.voiceInput}
            >
              {isListening ? (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
              ) : (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
              )}
            </button>

            <button
              className={`btn-icon ${isSpeaking ? 'btn-speaking' : ''}`}
              onClick={() => {
                if (isSpeaking) { stopSpeaking(); }
                else { setTtsEnabled(!ttsEnabled); }
              }}
              aria-label={isSpeaking ? strings.stopSpeaking : ttsEnabled ? strings.disableVoice : strings.enableVoice}
              title={isSpeaking ? strings.stop : ttsEnabled ? strings.voiceOn : strings.voiceOff}
            >
              {isSpeaking ? (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
              ) : ttsEnabled ? (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>
              ) : (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>
              )}
            </button>
          </div>
        </div>
      </aside>

      {/* Info modal */}
      {showInfo && (
        <div className="info-overlay" onClick={() => setShowInfo(false)}>
          <div className="info-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={strings.infoTitle}>
            <div className="info-modal-header">
              <h2>{strings.infoTitle}</h2>
              <button className="btn-info-close" onClick={() => setShowInfo(false)} aria-label={strings.infoClose}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
            <div className="info-modal-body">
              <div className="info-columns">
                <div className="info-section">
                  <div className="info-badge">{strings.infoBuiltFor}</div>
                  <h3>{strings.infoDevelopers}</h3>
                  <div className="info-devs">
                    <div className="info-dev-card">
                      <div className="info-dev-avatar">VA</div>
                      <span className="info-dev-name">{strings.infoDev1}</span>
                    </div>
                    <div className="info-dev-card">
                      <div className="info-dev-avatar">SM</div>
                      <span className="info-dev-name">{strings.infoDev2}</span>
                    </div>
                  </div>
                </div>
                <div className="info-section">
                  <h3>{strings.infoCapabilities}</h3>
                  <ul className="info-capabilities">
                    {strings.infoCapabilityList.map((item, i) => (
                      <li key={i}>{item}</li>
                    ))}
                  </ul>
                </div>
              </div>
              <div className={`info-credits ${creditsFade}`}>
                <h3>{strings.infoPoweredBy}</h3>
                <div className="info-credits-scroll" ref={creditsScrollRef} onScroll={handleCreditsScroll}>
                  <a className="info-credit-item" href="https://ai.google.dev/gemini-api" target="_blank" rel="noopener noreferrer">
                    <span className="info-credit-name">Gemini 2.5 Flash</span>
                    <span className="info-credit-role">LLM & Intent Engine</span>
                  </a>
                  <a className="info-credit-item" href="https://github.com/browser-use/browser-use" target="_blank" rel="noopener noreferrer">
                    <span className="info-credit-name">browser-use</span>
                    <span className="info-credit-role">Agentic Web Browsing</span>
                  </a>
                  <a className="info-credit-item" href="https://groq.com" target="_blank" rel="noopener noreferrer">
                    <span className="info-credit-name">Groq Whisper</span>
                    <span className="info-credit-role">Speech-to-Text</span>
                  </a>
                  <a className="info-credit-item" href="https://github.com/rany2/edge-tts" target="_blank" rel="noopener noreferrer">
                    <span className="info-credit-name">Edge TTS</span>
                    <span className="info-credit-role">Text-to-Speech</span>
                  </a>
                  <a className="info-credit-item" href="https://playwright.dev" target="_blank" rel="noopener noreferrer">
                    <span className="info-credit-name">Playwright</span>
                    <span className="info-credit-role">Page Capture</span>
                  </a>
                  <a className="info-credit-item" href="https://react.dev" target="_blank" rel="noopener noreferrer">
                    <span className="info-credit-name">React + Vite</span>
                    <span className="info-credit-role">Frontend</span>
                  </a>
                  <a className="info-credit-item" href="https://fastapi.tiangolo.com" target="_blank" rel="noopener noreferrer">
                    <span className="info-credit-name">FastAPI</span>
                    <span className="info-credit-role">Backend & WebSocket</span>
                  </a>
                  <a className="info-credit-item" href="https://cloud.google.com/run" target="_blank" rel="noopener noreferrer">
                    <span className="info-credit-name">Cloud Run</span>
                    <span className="info-credit-role">Deployment</span>
                  </a>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// --- Helpers ---

function isUrlInput(text) {
  const t = text.trim();
  return (
    t.startsWith('http://') ||
    t.startsWith('https://') ||
    /^[\w-]+\.[a-zA-Z]{2,}(\/\S*)?$/.test(t)
  );
}

function formatTime(ts) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function renderPlainText(text) {
  if (!text) return null;
  const paragraphs = text.split(/\n{2,}/);
  if (paragraphs.length > 1) return paragraphs.map((p, i) => <p key={i}>{p.trim()}</p>);
  return text.split('\n').map((line, i) => (
    <React.Fragment key={i}>{i > 0 && <br />}{line}</React.Fragment>
  ));
}

// --- Audio helpers ---

async function blobToWav(blob) {
  const arrayBuffer = await blob.arrayBuffer();
  const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);
  audioCtx.close();

  const targetRate = 16000;
  const sourceData = audioBuffer.getChannelData(0);
  const ratio = audioBuffer.sampleRate / targetRate;
  const newLength = Math.floor(sourceData.length / ratio);
  const resampled = new Float32Array(newLength);
  for (let i = 0; i < newLength; i++) {
    resampled[i] = sourceData[Math.floor(i * ratio)];
  }

  const pcmData = new Int16Array(newLength);
  for (let i = 0; i < newLength; i++) {
    const s = Math.max(-1, Math.min(1, resampled[i]));
    pcmData[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }

  const wavBuffer = new ArrayBuffer(44 + pcmData.byteLength);
  const view = new DataView(wavBuffer);
  writeString(view, 0, 'RIFF');
  view.setUint32(4, 36 + pcmData.byteLength, true);
  writeString(view, 8, 'WAVE');
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, targetRate, true);
  view.setUint32(28, targetRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeString(view, 36, 'data');
  view.setUint32(40, pcmData.byteLength, true);
  new Uint8Array(wavBuffer, 44).set(new Uint8Array(pcmData.buffer));
  return wavBuffer;
}

function writeString(view, offset, str) {
  for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}
