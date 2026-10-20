/* Music Ed Cafe: small progressive enhancements. The site works without JS;
   this adds the mobile menu, photo switching, and live search/filter/sort. */
(function () {
  'use strict';
  var d = document;

  // --- mobile baseline: no stray selection / long-press loupe on UI chrome.
  // Content marked .selectable (record details, descriptions, guides), links
  // and form fields keep normal behaviour.
  function el(t) { return t && t.nodeType === 3 ? t.parentElement : t; }
  function allowed(t) { t = el(t); return !!(t && t.closest && t.closest('.selectable, input, textarea, select, a')); }
  d.addEventListener('selectstart', function (e) { if (!allowed(e.target)) e.preventDefault(); });
  d.addEventListener('contextmenu', function (e) { if (!allowed(e.target)) e.preventDefault(); });

  // --- menu
  var btn = d.querySelector('.menu-btn'), nav = d.getElementById('site-nav');
  if (btn && nav) {
    btn.addEventListener('click', function () {
      var open = btn.getAttribute('aria-expanded') !== 'true';
      btn.setAttribute('aria-expanded', String(open));
      nav.classList.toggle('open', open);
    });
    d.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && btn.getAttribute('aria-expanded') === 'true') {
        btn.setAttribute('aria-expanded', 'false'); nav.classList.remove('open'); btn.focus();
      }
    });
  }

  // --- record photos
  var thumbs = d.querySelectorAll('.thumbs a');
  var main = d.querySelector('.record-main .sleeve-img');
  if (thumbs.length && main) {
    thumbs[0].setAttribute('aria-current', 'true');
    thumbs.forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault();
        main.removeAttribute('srcset');
        main.src = a.getAttribute('data-full');
        thumbs.forEach(function (x) { x.removeAttribute('aria-current'); });
        a.setAttribute('aria-current', 'true');
      });
    });
  }

  // --- back to top: floating button after ~one viewport of scroll on long pages;
  // hidden while the footer (which has its own Back to top button) is on screen.
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  function toTop() {
    window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' });
    var m = d.getElementById('main'); if (m) m.focus({ preventScroll: true });
  }
  var tt = d.getElementById('to-top'), foot = d.querySelector('.site-footer');
  d.querySelectorAll('[data-to-top]').forEach(function (b) { b.addEventListener('click', toTop); });
  if (tt) {
    tt.addEventListener('click', toTop);
    var footVisible = false;
    if (foot && 'IntersectionObserver' in window) {
      new IntersectionObserver(function (es) { footVisible = es[0].isIntersecting; upd(); }).observe(foot);
    }
    var upd = function () {
      var long = d.documentElement.scrollHeight > innerHeight * 2.2;
      tt.hidden = !(long && scrollY > innerHeight * 0.9 && !footVisible);
    };
    addEventListener('scroll', upd, { passive: true });
    addEventListener('resize', upd, { passive: true });
    upd();
  }

  // --- sticky search bar: add a shadow once it is stuck under the header
  var bar = d.getElementById('finder-bar'), sentinel = d.querySelector('.finder-sentinel');
  if (bar && sentinel && 'IntersectionObserver' in window) {
    var hh = (d.querySelector('.site-header') || {}).offsetHeight || 56;
    new IntersectionObserver(function (es) { bar.classList.toggle('is-stuck', !es[0].isIntersecting); },
      { rootMargin: '-' + hh + 'px 0px 0px 0px' }).observe(sentinel);
  }

  // --- search / filter / sort (shop + sold archive)
  var form = d.getElementById('finder');
  if (!form) return;
  var list = d.querySelector('.grid.results');
  if (!list) return;
  var cards = Array.prototype.slice.call(list.children);
  var q = d.getElementById('f-q'), g = d.getElementById('f-genre'), f = d.getElementById('f-format'),
      p = d.getElementById('f-price'), gr = d.getElementById('f-grade'), s = d.getElementById('f-sort');
  var countEl = d.getElementById('result-count'), none = d.getElementById('no-results'),
      fc = d.getElementById('filter-count'), panel = d.getElementById('filters'),
      toggle = form.querySelector('.filter-toggle');
  var text = {};   // id -> searchable text (from the JSON index, falls back to card text)
  cards.forEach(function (c) { text[c.dataset.id] = c.textContent.toLowerCase(); });
  var order = ['M', 'NM+', 'NM', 'NM-', 'M-', 'EX+', 'EX', 'VG++', 'VG+', 'VG', 'VG-', 'G+', 'G', 'F', 'P'];
  function rank(x) { var i = order.indexOf(x); return i < 0 ? 99 : i; }

  function readUrl() {
    var u = new URLSearchParams(location.search);
    if (u.get('q')) q.value = u.get('q');
    if (u.get('genre') && g) g.value = u.get('genre');
    if (u.get('format') && f) f.value = u.get('format');
    if (u.get('price') && p) p.value = u.get('price');
    if (u.get('grade') && gr) gr.value = u.get('grade');
    if (u.get('sort') && s) s.value = u.get('sort');
  }
  function writeUrl() {
    var u = new URLSearchParams();
    [['q', q], ['genre', g], ['format', f], ['price', p], ['grade', gr], ['sort', s]].forEach(function (kv) {
      if (kv[1] && kv[1].value && !(kv[0] === 'sort' && kv[1].value === 'new')) u.set(kv[0], kv[1].value);
    });
    var qs = u.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }
  function apply() {
    var terms = q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    var gv = g ? g.value : '', fv = f ? f.value : '', pv = p ? p.value : '', grv = gr ? gr.value : '';
    var lo = 0, hi = Infinity;
    if (pv) { var parts = pv.split('-'); lo = parseFloat(parts[0]) || 0; hi = parts[1] ? parseFloat(parts[1]) : Infinity; }
    var shown = 0;
    cards.forEach(function (c) {
      var t = text[c.dataset.id] || '';
      var ok = terms.every(function (w) { return t.indexOf(w) >= 0; });
      if (ok && gv) ok = (' ' + c.dataset.genres + ' ').indexOf(' ' + gv + ' ') >= 0;
      if (ok && fv) ok = (' ' + c.dataset.formats + ' ').indexOf(' ' + fv + ' ') >= 0;
      if (ok && pv) { var pr = parseFloat(c.dataset.price); ok = !isNaN(pr) && pr >= lo && pr < hi; }
      if (ok && grv) ok = c.dataset.grade && rank(c.dataset.grade) <= rank(grv);
      c.hidden = !ok;
      if (ok) shown++;
    });
    var sv = s ? s.value : 'new';
    var sorted = cards.slice().sort(function (a, b) {
      if (sv === 'price-asc') return (parseFloat(a.dataset.price) || 1e9) - (parseFloat(b.dataset.price) || 1e9);
      if (sv === 'price-desc') return (parseFloat(b.dataset.price) || 0) - (parseFloat(a.dataset.price) || 0);
      if (sv === 'artist') return a.dataset.artist.localeCompare(b.dataset.artist);
      return (b.dataset.date || '').localeCompare(a.dataset.date || '');
    });
    sorted.forEach(function (c) { list.appendChild(c); });
    countEl.textContent = shown + (shown === 1 ? ' record' : ' records') + (shown < cards.length ? ' of ' + cards.length : '');
    none.hidden = shown !== 0;
    if (bar && bar.classList.contains('is-stuck')) {
      var top = list.getBoundingClientRect().top, below = bar.getBoundingClientRect().bottom;
      if (top < below) window.scrollTo({ top: scrollY + top - below - 8, behavior: 'auto' });
    }
    var active = [gv, fv, pv, grv].filter(Boolean).length;
    if (fc) fc.textContent = active ? String(active) : '';
    writeUrl();
  }
  function reset() {
    q.value = '';
    [g, f, p, gr].forEach(function (x) { if (x) x.value = ''; });
    if (s) s.value = 'new';
    apply();
  }
  function sheet(open) {
    if (!panel || !toggle) return;
    panel.classList.toggle('open', open);
    toggle.setAttribute('aria-expanded', String(open));
    d.body.classList.toggle('sheet-open', open && matchMedia('(max-width: 899px)').matches);
    if (open) { var first = panel.querySelector('select'); if (first) first.focus(); } else toggle.focus();
  }
  form.addEventListener('input', apply);
  form.addEventListener('change', apply);
  var r1 = d.getElementById('f-reset'), r2 = d.getElementById('nr-reset');
  if (r1) r1.addEventListener('click', reset);
  if (r2) r2.addEventListener('click', reset);
  if (toggle) toggle.addEventListener('click', function () { sheet(!panel.classList.contains('open')); });
  var close = form.querySelector('.filters-close');
  if (close) close.addEventListener('click', function () { sheet(false); });
  d.addEventListener('keydown', function (e) { if (e.key === 'Escape' && panel && panel.classList.contains('open')) sheet(false); });
  d.addEventListener('click', function (e) {
    if (panel && panel.classList.contains('open') && !panel.contains(e.target) && e.target !== toggle && !toggle.contains(e.target)) sheet(false);
  });
  readUrl();
  apply();

  // richer search text (label, catalog #, year) from the JSON index, fetched
  // after load so it never holds up the page.
  function loadIndex() {
    fetch('/search-index.json').then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      if (!j) return;
      j.records.forEach(function (r) { if (r.id in text) text[r.id] = text[r.id] + ' ' + r.text; });
      if (q.value) apply();
    }).catch(function () {});
  }
  if (d.readyState === 'complete') loadIndex(); else window.addEventListener('load', loadIndex, { once: true });
})();

/* Merch reservation form: submit in place and show the result. Without JS the
   form posts normally and the server redirects to a thank-you page. */
(function () {
  'use strict';
  var d = document, form = d.querySelector('[data-merch-form]');
  if (!form || !window.fetch || !window.URLSearchParams) return;
  form.noValidate = true;
  var done = d.getElementById('reserve-done'), summary = form.querySelector('.rf-summary');
  var submit = form.querySelector('.rf-submit'), label = submit.textContent;
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function clear() {
    form.querySelectorAll('.rf-err').forEach(function (p) { p.hidden = true; p.textContent = ''; });
    form.querySelectorAll('[aria-invalid]').forEach(function (x) { x.removeAttribute('aria-invalid'); });
    summary.hidden = true; summary.textContent = '';
  }
  function showErrors(errs) {
    var first = null, keys = Object.keys(errs);
    keys.forEach(function (k) {
      var p = d.getElementById('err-' + k);
      if (p) { p.textContent = errs[k]; p.hidden = false; }
      var f = form.querySelector('[name="' + k + '"]');
      if (f) { f.setAttribute('aria-invalid', 'true'); if (p) f.setAttribute('aria-describedby', p.id); if (!first) first = f; }
    });
    summary.innerHTML = '<p><strong>Please check ' + (keys.length === 1 ? 'one thing' : keys.length + ' things') + ':</strong></p><ul>' +
      keys.map(function (k) { return '<li>' + esc(errs[k]) + '</li>'; }).join('') + '</ul>';
    summary.hidden = false;
    (first || summary).focus();
  }
  function local() {
    var e = {};
    form.querySelectorAll('fieldset[data-field]').forEach(function (fs) {
      var n = fs.getAttribute('data-field');
      if (!form.querySelector('[name="' + n + '"]:checked')) e[n] = 'Please choose a ' + fs.querySelector('legend').textContent.toLowerCase() + '.';
    });
    var el = form.elements;
    if (!el.namedItem('name').value.trim()) e.name = 'Please tell us your name.';
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(el.namedItem('email').value.trim())) e.email = 'Please enter a valid email address, like you@example.com.';
    if (!el.namedItem('consent').checked) e.consent = 'Please tick the box so we can email you about your reservation.';
    return e;
  }
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    clear();
    var e = local();
    if (Object.keys(e).length) { showErrors(e); return; }
    submit.disabled = true; submit.textContent = 'Reserving...';
    fetch(form.action, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams(new FormData(form)).toString()
    }).then(function (r) { return r.json().catch(function () { return { ok: false, message: 'Something went wrong on our side. Please try again in a minute.' }; }); })
      .then(function (j) {
        submit.disabled = false; submit.textContent = label;
        if (j.ok) {
          var r = j.reservation || {}, opts = r.options || {};
          var rows = [['Item', r.item_name]].concat(Object.keys(opts).map(function (k) { return [k.charAt(0).toUpperCase() + k.slice(1), opts[k]]; }))
            .concat([['Quantity', r.qty], ['Name', r.name]]);
          if (r.note) rows.push(['Note', r.note]);
          done.innerHTML = '<p class="rd-kicker">Reserved</p><h2>Thanks' + (r.name ? ', ' + esc(r.name.split(' ')[0]) : '') + '! Your reservation is in.</h2>' +
            (r.item_name ? '<dl class="specs rd-specs">' + rows.map(function (x) { return '<div><dt>' + esc(x[0]) + '</dt><dd>' + esc(x[1]) + '</dd></div>'; }).join('') + '</dl>' : '') +
            '<p>No payment now. We\'ll email you when your item is ready to pay for and ship. Reservations aren\'t binding.</p>' +
            '<p class="empty-actions"><a class="btn btn-buy" href="/merch/">See more merch</a> <a class="btn btn-ghost" href="/shop/">Shop records</a></p>';
          form.hidden = true; done.hidden = false; done.focus();
          done.scrollIntoView({ block: 'start' });
        } else if (j.errors) {
          showErrors(j.errors);
        } else {
          summary.innerHTML = '<p>' + esc(j.message || 'Something went wrong. Please try again.') + '</p>';
          summary.hidden = false; summary.focus();
        }
      }).catch(function () {
        submit.disabled = false; submit.textContent = label;
        summary.innerHTML = '<p>We couldn\'t reach the server. Check your connection and try again.</p>';
        summary.hidden = false; summary.focus();
      });
  });
})();
