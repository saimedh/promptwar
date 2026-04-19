/* ============================================================
   PromptWars — app.js
   Scoring UI logic — calls the FastAPI /score endpoint
   ============================================================ */

'use strict';

// ── SVG gradient injected once ──────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const svgNS = 'http://www.w3.org/2000/svg';
  const defs = document.createElementNS(svgNS, 'svg');
  defs.style.cssText = 'position:absolute;width:0;height:0';
  defs.innerHTML = `
    <defs>
      <linearGradient id="ring-gradient" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%"   stop-color="#8b5cf6"/>
        <stop offset="100%" stop-color="#06b6d4"/>
      </linearGradient>
    </defs>`;
  document.body.prepend(defs);
});

// ── DOM refs ────────────────────────────────────────────────
const form          = document.getElementById('score-form');
const promptInput   = document.getElementById('prompt-input');
const taskInput     = document.getElementById('task-input');
const apiUrlInput   = document.getElementById('api-url-input');
const promptChars   = document.getElementById('prompt-chars');
const scoreBtn      = document.getElementById('score-btn');

const emptyState    = document.getElementById('empty-state');
const loadingState  = document.getElementById('loading-state');
const errorState    = document.getElementById('error-state');
const errorMsg      = document.getElementById('error-message');
const retryBtn      = document.getElementById('retry-btn');
const scoreResults  = document.getElementById('score-results');

const overallText   = document.getElementById('overall-score-text');
const ringFill      = document.getElementById('ring-fill');
const cacheBadge    = document.getElementById('cache-badge');
const hashDisplay   = document.getElementById('prompt-hash-display');
const dimList       = document.getElementById('dimension-list');
const strengthsList = document.getElementById('strengths-list');
const improveList   = document.getElementById('improvements-list');
const rawJson       = document.getElementById('raw-json');

// ── Char counter ─────────────────────────────────────────────
promptInput.addEventListener('input', () => {
  promptChars.textContent = promptInput.value.length;
});

// ── State helpers ────────────────────────────────────────────
function showState(name) {
  [emptyState, loadingState, errorState, scoreResults].forEach(el => el.classList.add('hidden'));
  document.getElementById(`${name}-state`)?.classList.remove('hidden');
  if (name === 'score') scoreResults.classList.remove('hidden');
}

// ── Colour helpers ───────────────────────────────────────────
function scoreClass(s) {
  if (s >= 7) return 'score-high';
  if (s >= 4) return 'score-medium';
  return 'score-low';
}
function barClass(s) {
  if (s >= 7) return 'bar-high';
  if (s >= 4) return 'bar-medium';
  return 'bar-low';
}

// ── Animated ring ────────────────────────────────────────────
function animateRing(score) {
  // circumference = 2π×50 ≈ 314
  const pct = Math.min(100, Math.max(0, score)) / 100;
  const offset = 314 * (1 - pct);
  ringFill.style.strokeDashoffset = offset;
}

// ── Render results ───────────────────────────────────────────
function renderResults(data) {
  // Overall score
  overallText.textContent = data.overall_score;
  overallText.className   = `ring-score ${scoreClass(data.overall_score / 10)}`;
  animateRing(data.overall_score);

  // Cache badge
  if (data.cache_hit) {
    cacheBadge.textContent = '⚡ Cache HIT';
    cacheBadge.className   = 'cache-badge hit';
  } else {
    cacheBadge.textContent = '🤖 Gemini scored';
    cacheBadge.className   = 'cache-badge miss';
  }

  hashDisplay.textContent = `hash: ${data.prompt_hash}`;

  // Dimensions
  dimList.innerHTML = '';
  (data.dimensions || []).forEach(dim => {
    const pct = (dim.score / 10) * 100;
    const item = document.createElement('div');
    item.className = 'dimension-item';
    item.setAttribute('role', 'listitem');
    item.innerHTML = `
      <div class="dim-row">
        <span class="dim-name">${escHtml(dim.dimension)}</span>
        <span class="dim-score-val ${scoreClass(dim.score)}">${dim.score}/10</span>
      </div>
      <div class="dim-bar-track" role="progressbar" aria-valuenow="${dim.score}" aria-valuemin="0" aria-valuemax="10">
        <div class="dim-bar-fill ${barClass(dim.score)}" data-width="${pct}"></div>
      </div>
      <span class="dim-reason">${escHtml(dim.reason)}</span>`;
    dimList.appendChild(item);
  });

  // Animate bars after paint
  requestAnimationFrame(() => {
    document.querySelectorAll('.dim-bar-fill').forEach(bar => {
      bar.style.width = bar.dataset.width + '%';
    });
  });

  // Strengths
  strengthsList.innerHTML = '';
  (data.strengths || []).forEach(s => {
    const li = document.createElement('li');
    li.textContent = s;
    strengthsList.appendChild(li);
  });
  if (!data.strengths?.length) {
    const li = document.createElement('li');
    li.textContent = 'None identified.';
    li.style.opacity = '0.5';
    strengthsList.appendChild(li);
  }

  // Improvements
  improveList.innerHTML = '';
  (data.improvements || []).forEach(s => {
    const li = document.createElement('li');
    li.textContent = s;
    improveList.appendChild(li);
  });
  if (!data.improvements?.length) {
    const li = document.createElement('li');
    li.textContent = 'None suggested — great prompt!';
    li.style.opacity = '0.5';
    improveList.appendChild(li);
  }

  // Raw JSON
  rawJson.textContent = JSON.stringify(data, null, 2);

  showState('score');
}

// ── Score fetch ──────────────────────────────────────────────
async function scorePrompt() {
  const prompt  = promptInput.value.trim();
  const task    = taskInput.value.trim();
  const baseUrl = (apiUrlInput.value.trim() || 'http://localhost:8080').replace(/\/$/, '');

  if (!prompt) { promptInput.focus(); return; }
  if (!task)   { taskInput.focus();   return; }

  showState('loading');
  scoreBtn.disabled = true;
  scoreBtn.classList.add('loading');

  try {
    const res = await fetch(`${baseUrl}/score`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ prompt, task }),
    });

    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();
    renderResults(data);

  } catch (err) {
    errorMsg.textContent = err.message || 'Unknown error. Is the API running?';
    showState('error');
    console.error('[PromptWars]', err);
  } finally {
    scoreBtn.disabled = false;
    scoreBtn.classList.remove('loading');
  }
}

// ── Demo mode (no real API) ──────────────────────────────────
function loadDemoData() {
  const demo = {
    overall_score: 88.0,
    dimensions: [
      { dimension: 'Clarity',        score: 9, reason: 'The instruction is unambiguous and easy to parse.' },
      { dimension: 'Specificity',    score: 9, reason: 'Three bullet points and a 20-word cap give precise constraints.' },
      { dimension: 'Task alignment', score: 9, reason: 'Directly targets summarisation with clear scope.' },
      { dimension: 'Output format',  score: 8, reason: 'Bullet format is specified but no example provided.' },
      { dimension: 'Conciseness',    score: 9, reason: 'No unnecessary words; every clause adds constraint.' },
    ],
    strengths:    ['Precise output constraints (3 bullets, 20 words)', 'Audience-aware framing', 'Format explicitly stated'],
    improvements: ['Provide a sample bullet to anchor style', 'Specify language or tone (formal vs casual)'],
    cache_hit:    false,
    prompt_hash:  'a3f8c21b0e947d6f2c81a04b',
  };
  renderResults(demo);
}

// ── Event listeners ──────────────────────────────────────────
form.addEventListener('submit', e => { e.preventDefault(); scorePrompt(); });
retryBtn.addEventListener('click', scorePrompt);

// Keyboard shortcut: Ctrl/Cmd + Enter
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    scorePrompt();
  }
});

// ── XSS guard ────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}

// ── Auto-load demo on first visit ───────────────────────────
window.addEventListener('load', () => {
  // Pre-fill example to guide the user
  if (!promptInput.value) {
    promptInput.value = 'Summarise the following article in three bullet points, each under 20 words, targeting a non-technical audience.';
    promptChars.textContent = promptInput.value.length;
  }
  if (!taskInput.value) {
    taskInput.value = 'text summarisation';
  }
});
