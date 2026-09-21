"""ui/head.py — Gradio 页面 head 注入的增强 JS

包含：实时时钟、Ctrl+Enter 提交、复制按钮委托、科大讯飞语音输入状态机。
全部通过 gr.Blocks(..., head=HEAD_JS) 注入，gr.HTML 不执行 script。
"""
from __future__ import annotations

HEAD_JS = """
<script>
(function () {
  if (window.__GRADIO_ENHANCED__) return;
  window.__GRADIO_ENHANCED__ = true;

  var voiceTextarea = function () {
    return document.querySelector('#mission-input textarea, .input-box textarea');
  };
  var voiceCopyEl = function () { return document.getElementById('voice-copy'); };
  var voiceBtn = function () { return document.getElementById('voice-button'); };
  var setVoiceCopy = function (text) {
    var el = voiceCopyEl();
    if (el) el.textContent = text;
  };

  /* ── 实时时钟 ── */
  function tickClock() {
    var el = document.getElementById('clock');
    if (el) {
      el.textContent = new Intl.DateTimeFormat('zh-CN', {
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
      }).format(new Date());
    }
  }
  tickClock();
  setInterval(tickClock, 1000);

  /* ── 复制按钮（document 级委托）── */
  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-copy-el]');
    if (!btn) return;
    var target = document.querySelector(btn.getAttribute('data-copy-el'));
    if (!target) return;
    var text = target.innerText || target.textContent || '';
    var original = btn.textContent;
    var done = function () {
      btn.textContent = '✅ 已复制';
      setTimeout(function () { btn.textContent = original; }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else {
      fallbackCopy(text);
      done();
    }
  });

  /* ── Ctrl+Enter 提交（capture 阶段，阻断 Gradio 原生 Enter 双提交）── */
  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      var ta = voiceTextarea();
      if (ta && document.activeElement === ta) {
        e.preventDefault();
        e.stopPropagation();
        var btn = document.getElementById('submit-btn');
        if (btn) btn.click();
      }
    }
  }, true);

  /* ── 科大讯飞语音输入 ── */
  var voice = null;

  function updateVoiceText() {
    var ta = voiceTextarea();
    if (!ta || !voice) return;
    ta.value = voice.baseText + voice.transcript;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function voiceRequest(path, payload) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    }).then(function (resp) {
      return resp.json().then(function (body) {
        if (!resp.ok) throw new Error(body.error || '语音服务请求失败');
        return body;
      });
    });
  }

  function pcm16Base64(input, sourceRate) {
    var ratio = sourceRate / 16000;
    var length = Math.max(1, Math.round(input.length / ratio));
    var output = new Int16Array(length);
    for (var index = 0; index < length; index += 1) {
      var start = Math.floor(index * ratio);
      var end = Math.min(input.length, Math.floor((index + 1) * ratio));
      var sum = 0;
      for (var cursor = start; cursor < Math.max(start + 1, end); cursor += 1) sum += input[cursor] || 0;
      var sample = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
      output[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }
    var bytes = new Uint8Array(output.buffer);
    var binary = "";
    for (var b = 0; b < bytes.length; b += 1) binary += String.fromCharCode(bytes[b]);
    return btoa(binary);
  }

  function startVoice() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.AudioContext) {
      throw new Error('当前浏览器不支持麦克风录音');
    }
    setVoiceCopy('正在连接科大讯飞...');
    return navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
    }).then(function (mic) {
      return voiceRequest('/api/voice/start').then(function (started) {
        var ctx = new AudioContext();
        var source = ctx.createMediaStreamSource(mic);
        return ctx.resume().then(function () {
          var processor = ctx.createScriptProcessor(8192, 1, 1);
          voice = {
            sessionId: started.session_id,
            microphone: mic,
            context: ctx,
            source: source,
            processor: processor,
            baseText: (voiceTextarea() || {}).value || '',
            transcript: '',
            queue: Promise.resolve(),
            capturing: true,
            failed: false
          };
          source.connect(processor);
          processor.connect(ctx.destination);
          processor.onaudioprocess = function (event) {
            if (!voice.capturing) return;
            var audio = pcm16Base64(event.inputBuffer.getChannelData(0), ctx.sampleRate);
            voice.queue = voice.queue.then(function () {
              return voiceRequest('/api/voice/chunk', { session_id: voice.sessionId, audio: audio })
                .then(function (result) {
                  voice.transcript = result.text || voice.transcript || '';
                  updateVoiceText();
                  setVoiceCopy(result.text ? '实时识别：' + result.text.slice(-24) : '正在听写...');
                });
            }).catch(function (err) {
              voice.failed = true;
              voice.capturing = false;
              setVoiceCopy('识别失败：' + err.message);
            });
          };
          var btn = voiceBtn();
          if (btn) btn.classList.add('recording');
          setVoiceCopy('正在听写，再次点击结束');
        });
      }, function (err) {
        mic.getTracks().forEach(function (t) { t.stop(); });
        throw err;
      });
    });
  }

  function stopVoice() {
    if (!voice) return Promise.resolve();
    voice.capturing = false;
    try { voice.processor.disconnect(); } catch (e) {}
    try { voice.source.disconnect(); } catch (e) {}
    voice.microphone.getTracks().forEach(function (t) { t.stop(); });
    return voice.queue.then(function () {
      return voiceRequest('/api/voice/finish', { session_id: voice.sessionId })
        .then(function (result) {
          voice.transcript = result.text || voice.transcript;
          updateVoiceText();
          setVoiceCopy(result.text
            ? (voice.failed ? '语音识别完成，末段上传异常' : '科大讯飞识别完成')
            : '未识别到有效语音');
        });
    }).catch(function (err) {
      setVoiceCopy('语音识别失败：' + err.message);
    }).finally(function () {
      try { voice.context.close(); } catch (e) {}
      voice = null;
      var btn = voiceBtn();
      if (btn) btn.classList.remove('recording');
    });
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('#voice-button');
    if (!btn) return;
    if (voice) {
      stopVoice();
    } else {
      startVoice().catch(function (err) {
        setVoiceCopy('语音识别失败：' + err.message);
        var b = voiceBtn();
        if (b) b.classList.remove('recording');
      });
    }
  });
})();
</script>
"""
