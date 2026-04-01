/**
 * Base do Flask (Socket.IO + /api/*).
 * - Se a página for servida pelo Live Server ou outra porta estática, usa host:5000.
 * - Pode forçar: window.__VOICEOVER_API__ = 'http://127.0.0.1:5000';
 */
function getApiBase() {
    if (typeof window.__VOICEOVER_API__ === "string" && window.__VOICEOVER_API__.trim()) {
        return window.__VOICEOVER_API__.replace(/\/$/, "");
    }
    var proto = window.location.protocol;
    var host = window.location.hostname;
    var port = window.location.port;
    if (proto === "file:") {
        return "http://127.0.0.1:5000";
    }
    var staticPorts = {
        "5500": 1,
        "5501": 1,
        "5502": 1,
        "8080": 1,
        "5173": 1,
        "4173": 1,
        "3000": 1,
    };
    if (staticPorts[port]) {
        return proto + "//" + host + ":5000";
    }
    return "";
}

var API_BASE = getApiBase();

function apiUrl(path) {
    var p = path.charAt(0) === "/" ? path : "/" + path;
    return (API_BASE || "") + p;
}

var socket = API_BASE
    ? io(API_BASE, { transports: ["polling", "websocket"] })
    : io({ transports: ["polling", "websocket"] });

var liveRecognition = null;

var LANG_LABELS = { en: "EN", es: "ES", fr: "FR", pt: "PT", it: "IT" };

var MAX_RECORD_MS = 120000;

var voiceStream = null;
var voiceMediaRecorder = null;
var voiceChunks = [];
var voiceTimerInterval = null;
var voiceMaxTimeout = null;
var voiceRecordingStartedAt = 0;
var lastCapturedBlob = null;
var lastCapturedObjectUrl = null;
/** Só true após "Enviar tradução" com áudio; evita tocar gravação na resposta do chat por texto. */
var pendingVoiceResponse = false;

function getOutputLang() {
    var g = document.getElementById("outputLangGroup");
    if (!g) return "all";
    var sel = g.querySelector(".md-filter-chip.is-selected");
    var v = sel && sel.getAttribute("data-value");
    return v || "all";
}

var TTS_VOICE_STORAGE = "voiceover_tts_voice_id";

function getTtsVoice() {
    var sel = document.getElementById("ttsVoiceSelect");
    if (!sel) return "";
    return (sel.value || "").trim();
}

function loadTtsVoices() {
    var sel = document.getElementById("ttsVoiceSelect");
    if (!sel) return;

    function persist() {
        var v = (sel.value || "").trim();
        if (v) {
            try {
                localStorage.setItem(TTS_VOICE_STORAGE, v);
            } catch (e) {
                /* ignore */
            }
        } else {
            try {
                localStorage.removeItem(TTS_VOICE_STORAGE);
            } catch (e2) {
                /* ignore */
            }
        }
    }

    sel.addEventListener("change", persist);

    fetch(apiUrl("/api/tts-voices"))
        .then(function (r) {
            return r.json();
        })
        .then(function (j) {
            var voices = (j && j.voices) || [];
            var saved = "";
            try {
                saved = localStorage.getItem(TTS_VOICE_STORAGE) || "";
            } catch (e) {
                saved = "";
            }

            sel.innerHTML = "";
            var def = document.createElement("option");
            def.value = "";
            def.textContent = "Automático (voz padrão por idioma)";
            sel.appendChild(def);

            var byLocale = {};
            for (var i = 0; i < voices.length; i++) {
                var v = voices[i];
                var loc = v.locale || "—";
                if (!byLocale[loc]) byLocale[loc] = [];
                byLocale[loc].push(v);
            }
            var locs = Object.keys(byLocale).sort();
            for (var li = 0; li < locs.length; li++) {
                var loc = locs[li];
                var og = document.createElement("optgroup");
                og.label = loc;
                var list = byLocale[loc];
                for (var k = 0; k < list.length; k++) {
                    var vo = list[k];
                    var o = document.createElement("option");
                    o.value = vo.id || "";
                    var g = vo.gender ? vo.gender + " · " : "";
                    o.textContent = g + (vo.label || vo.id || "");
                    og.appendChild(o);
                }
                sel.appendChild(og);
            }

            if (saved) {
                sel.value = saved;
                if (sel.value !== saved) {
                    try {
                        localStorage.removeItem(TTS_VOICE_STORAGE);
                    } catch (e2) {
                        /* ignore */
                    }
                }
            }
        })
        .catch(function () {
            sel.innerHTML =
                '<option value="">Não foi possível carregar vozes (verifique o servidor)</option>';
        });
}

function revokeCapturedUrl() {
    if (lastCapturedObjectUrl) {
        URL.revokeObjectURL(lastCapturedObjectUrl);
        lastCapturedObjectUrl = null;
    }
}

function formatVoiceTime(ms) {
    var s = Math.floor(ms / 1000);
    var m = Math.floor(s / 60);
    s = s % 60;
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
}

function setVoiceTimerDisplay(ms) {
    var el = document.getElementById("voiceTimer");
    if (el) el.textContent = formatVoiceTime(ms);
}

function updateVoiceUiRecording(active) {
    var wrap = document.getElementById("voiceMeterWrap");
    var btnStart = document.getElementById("btnRecStart");
    var btnStop = document.getElementById("btnRecStop");
    if (wrap) wrap.classList.toggle("is-active", active);
    if (btnStart) {
        btnStart.disabled = active;
        btnStart.classList.toggle("is-recording", active);
    }
    if (btnStop) btnStop.disabled = !active;
}

function setWhisperTranscript(text) {
    var el = document.getElementById("transcriptWhisper");
    if (!el) return;
    el.textContent = text && String(text).trim() ? String(text).trim() : "—";
}

function setLiveTranscript(text) {
    var el = document.getElementById("transcriptLive");
    if (!el) return;
    el.textContent = text || "";
}

function stopLiveTranscription() {
    if (!liveRecognition) return;
    try {
        liveRecognition.onresult = null;
        liveRecognition.onerror = null;
        liveRecognition.stop();
    } catch (e) {
        /* ignore */
    }
    liveRecognition = null;
}

function startLiveTranscription() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    var liveEl = document.getElementById("transcriptLive");
    var unsup = document.getElementById("transcriptUnsupported");
    if (!SR) {
        if (unsup) unsup.hidden = false;
        return null;
    }
    if (unsup) unsup.hidden = true;
    if (liveEl) liveEl.textContent = "";

    var r = new SR();
    var lang = document.documentElement.lang || "pt-BR";
    r.lang = lang.indexOf("pt") === 0 ? "pt-BR" : lang;
    r.continuous = true;
    r.interimResults = true;
    r.maxAlternatives = 1;

    r.onresult = function (e) {
        var line = "";
        for (var i = e.resultIndex; i < e.results.length; i++) {
            line += e.results[i][0].transcript;
        }
        if (liveEl) liveEl.textContent = line;
    };

    r.onerror = function (ev) {
        if (!liveEl) return;
        if (ev.error === "no-speech" || ev.error === "aborted") return;
        if (ev.error === "not-allowed") {
            liveEl.textContent = "Permissão de microfone negada para o reconhecimento de voz.";
        }
    };

    try {
        r.start();
    } catch (e) {
        if (unsup) unsup.hidden = false;
        return null;
    }
    return r;
}

function clearVoiceTimers() {
    if (voiceTimerInterval) {
        clearInterval(voiceTimerInterval);
        voiceTimerInterval = null;
    }
    if (voiceMaxTimeout) {
        clearTimeout(voiceMaxTimeout);
        voiceMaxTimeout = null;
    }
}

async function beginVoiceCapture() {
    var btnListen = document.getElementById("btnRecListen");
    var btnSend = document.getElementById("btnRecSend");
    if (btnListen) btnListen.disabled = true;
    if (btnSend) btnSend.disabled = true;

    revokeCapturedUrl();
    lastCapturedBlob = null;
    voiceChunks = [];

    voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });

    setLiveTranscript("");
    stopLiveTranscription();
    liveRecognition = startLiveTranscription();

    var mime = "audio/webm";
    if (typeof MediaRecorder !== "undefined") {
        if (MediaRecorder.isTypeSupported("audio/webm;codecs=opus")) {
            mime = "audio/webm;codecs=opus";
        } else if (MediaRecorder.isTypeSupported("audio/webm")) {
            mime = "audio/webm";
        }
    }

    voiceMediaRecorder = new MediaRecorder(voiceStream, { mimeType: mime });
    voiceMediaRecorder.ondataavailable = function (event) {
        if (event.data && event.data.size) voiceChunks.push(event.data);
    };

    voiceMediaRecorder.onstop = function () {
        clearVoiceTimers();
        if (voiceStream) {
            voiceStream.getTracks().forEach(function (t) {
                t.stop();
            });
            voiceStream = null;
        }
        stopLiveTranscription();

        var blob = new Blob(voiceChunks, {
            type: voiceMediaRecorder && voiceMediaRecorder.mimeType ? voiceMediaRecorder.mimeType : "audio/webm",
        });
        voiceChunks = [];
        lastCapturedBlob = blob;
        revokeCapturedUrl();
        lastCapturedObjectUrl = URL.createObjectURL(blob);

        var bl = document.getElementById("btnRecListen");
        var bs = document.getElementById("btnRecSend");
        if (bl) bl.disabled = false;
        if (bs) bs.disabled = false;

        updateVoiceUiRecording(false);
        setVoiceTimerDisplay(0);

        var hint = document.getElementById("statusHint");
        if (hint) hint.textContent = "Gravação pronta. Ouça a prévia ou envie para tradução.";
    };

    voiceRecordingStartedAt = Date.now();
    setVoiceTimerDisplay(0);
    voiceTimerInterval = setInterval(function () {
        setVoiceTimerDisplay(Date.now() - voiceRecordingStartedAt);
    }, 200);

    voiceMaxTimeout = setTimeout(function () {
        stopVoiceCapture();
    }, MAX_RECORD_MS);

    voiceMediaRecorder.start();
    updateVoiceUiRecording(true);

    var hint = document.getElementById("statusHint");
    if (hint) hint.textContent = "Gravando… use Parar quando terminar.";
}

function stopVoiceCapture() {
    if (!voiceMediaRecorder || voiceMediaRecorder.state === "inactive") return;
    clearVoiceTimers();
    try {
        voiceMediaRecorder.stop();
    } catch (e) {
        /* ignore */
    }
    voiceMediaRecorder = null;
}

function playCapturedVoice() {
    if (!lastCapturedObjectUrl) return;
    var a = new Audio(lastCapturedObjectUrl);
    a.play();
}

function sendCapturedVoice() {
    if (!lastCapturedBlob) return;
    pendingVoiceResponse = true;

    var reader = new FileReader();
    reader.readAsDataURL(lastCapturedBlob);
    reader.onloadend = function () {
        var base64 = reader.result.split(",")[1];
        setWhisperTranscript("Processando…");
        var hint = document.getElementById("statusHint");
        if (hint) hint.textContent = "Transcrevendo no servidor…";
        socket.emit("voice_message", {
            username: document.getElementById("username")
                ? document.getElementById("username").value
                : "",
            audio: base64,
            output_lang: getOutputLang(),
            tts_voice: getTtsVoice(),
        });
    };
}

function playTranslationAudioSequentially(data) {
    if (!data.audio) return;
    var langs = Object.keys(data.audio);
    var i = 0;
    function playNext() {
        if (i >= langs.length) return;
        var lang = langs[i++];
        var b64 = data.audio[lang];
        if (!b64 || typeof b64 !== "string" || !b64.length) {
            playNext();
            return;
        }
        var a = new Audio("data:audio/mp3;base64," + b64);
        a.onended = playNext;
        a.onerror = playNext;
        a.play();
    }
    playNext();
}

function playResponseWithCapturedVoice(data) {
    var uname =
        (document.getElementById("username") &&
            document.getElementById("username").value) ||
        "";
    var fromMe = (data.username || "") === uname;

    var chk = document.getElementById("chkPlayOriginalFirst");
    var playOriginal =
        pendingVoiceResponse &&
        fromMe &&
        chk &&
        chk.checked &&
        lastCapturedObjectUrl;
    if (fromMe) pendingVoiceResponse = false;

    if (playOriginal) {
        var a0 = new Audio(lastCapturedObjectUrl);
        a0.onended = function () {
            playTranslationAudioSequentially(data);
        };
        a0.onerror = function () {
            playTranslationAudioSequentially(data);
        };
        a0.play();
    } else {
        playTranslationAudioSequentially(data);
    }
}

socket.on("transcription", function (payload) {
    var hint = document.getElementById("statusHint");
    if (hint) hint.textContent = "";
    if (payload && payload.error) {
        setWhisperTranscript("Erro: " + payload.error);
        return;
    }
    var t = payload && payload.text != null ? payload.text : "";
    setWhisperTranscript(t);
});

function appendMessage(data) {
    var chat = document.getElementById("chat");
    if (!chat) return;

    var empty = chat.querySelector(".md-message__empty");
    if (empty) empty.remove();

    var article = document.createElement("article");
    article.className = "md-message";
    article.setAttribute("role", "article");

    var user = document.createElement("p");
    user.className = "md-message__user";
    user.textContent = data.username || "—";
    article.appendChild(user);

    function addBlock(labelKey, text, audioBase64) {
        var p = document.createElement("div");
        p.className = "md-message__block";
        var span = document.createElement("span");
        span.className = "md-message__label";
        span.textContent = labelKey;
        p.appendChild(span);
        p.appendChild(document.createTextNode(text));

        if (labelKey !== "Original") {
            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "md-message__play";
            btn.textContent = "Play";
            if (!audioBase64) {
                btn.disabled = true;
            } else {
                btn.addEventListener("click", function () {
                    var a = new Audio("data:audio/mp3;base64," + audioBase64);
                    a.play();
                });
            }
            p.appendChild(btn);
        }
        article.appendChild(p);
    }

    var orig =
        data.original && String(data.original).trim()
            ? data.original
            : "(nenhuma fala detectada — fale mais alto ou mais perto do microfone)";
    addBlock("Original", orig, null);

    var langs =
        data.output_langs && data.output_langs.length
            ? data.output_langs
            : ["en", "es", "fr", "pt", "it"];
    for (var j = 0; j < langs.length; j++) {
        var code = langs[j];
        var lbl = LANG_LABELS[code] || String(code).toUpperCase();
        var tv =
            data.translations && data.translations[code] != null
                ? data.translations[code]
                : "—";
        var b64 = data.audio && data.audio[code] ? data.audio[code] : null;
        addBlock(lbl, tv, b64);
    }

    chat.appendChild(article);
    chat.scrollTop = chat.scrollHeight;
}

socket.on("receive_translation", function (data) {
    appendMessage(data);
    playResponseWithCapturedVoice(data);
});

function sendTextMessage() {
    var input = document.getElementById("messageInput");
    if (!input) return;
    var raw = input.value;
    var text = raw.replace(/^\s+|\s+$/g, "");
    if (!text) return;

    pendingVoiceResponse = false;

    setWhisperTranscript("Processando…");
    socket.emit("text_message", {
        username: document.getElementById("username")
            ? document.getElementById("username").value
            : "",
        text: raw,
        output_lang: getOutputLang(),
        tts_voice: getTtsVoice(),
    });
    input.value = "";
    input.focus();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadTtsVoices);
} else {
    loadTtsVoices();
}

(function initVoiceCapture() {
    var start = document.getElementById("btnRecStart");
    var stop = document.getElementById("btnRecStop");
    var listen = document.getElementById("btnRecListen");
    var send = document.getElementById("btnRecSend");
    if (start) start.addEventListener("click", beginVoiceCapture);
    if (stop) stop.addEventListener("click", stopVoiceCapture);
    if (listen) listen.addEventListener("click", playCapturedVoice);
    if (send) send.addEventListener("click", sendCapturedVoice);
})();

(function initOutputLangChips() {
    var g = document.getElementById("outputLangGroup");
    if (!g) return;
    g.addEventListener("click", function (ev) {
        var btn = ev.target.closest(".md-filter-chip");
        if (!btn || !g.contains(btn)) return;
        var val = btn.getAttribute("data-value");
        if (!val) return;
        var chips = g.querySelectorAll(".md-filter-chip");
        for (var i = 0; i < chips.length; i++) {
            var c = chips[i];
            var on = c === btn;
            c.classList.toggle("is-selected", on);
            c.setAttribute("aria-pressed", on ? "true" : "false");
        }
    });
})();

(function initChatComposer() {
    var input = document.getElementById("messageInput");
    if (!input) return;
    input.addEventListener("keydown", function (ev) {
        if (ev.key !== "Enter") return;
        if (ev.shiftKey) return;
        ev.preventDefault();
        sendTextMessage();
    });
})();

(function initEmptyState() {
    var chat = document.getElementById("chat");
    if (!chat || chat.children.length) return;
    var p = document.createElement("p");
    p.className = "md-message__empty";
    p.textContent =
        "Nenhuma mensagem ainda. Digite abaixo ou use Captura de voz para enviar áudio.";
    chat.appendChild(p);
})();
