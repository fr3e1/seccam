import { $ } from './ui.js';

const tabs = document.querySelectorAll('.tab');
const panels = document.querySelectorAll('[data-panel]');

function select(id) {
  for (const t of tabs) {
    const on = t.dataset.tab === id;
    t.classList.toggle('tab-active', on);
    t.setAttribute('aria-selected', String(on));
  }
  for (const p of panels) p.classList.toggle('hidden', p.dataset.panel !== id);
}

for (const t of tabs) t.addEventListener('click', () => select(t.dataset.tab));
const ua = navigator.userAgent;
select(/iPhone|iPad/.test(ua) ? 'iphone' : /Linux/.test(ua) && !/Android/.test(ua) ? 'linux' : 'android');

const camera = location.origin + '/camera';
for (const el of document.querySelectorAll('.origin-camera')) el.textContent = camera;
$('#ca-link').setAttribute('href', '/ca.crt');
