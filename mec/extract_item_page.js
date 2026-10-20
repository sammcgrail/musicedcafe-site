(() => {
  const T = el => el ? el.innerText.replace(/\s+/g,' ').trim() : null;
  const out = {title: T(document.querySelector('h1.x-item-title__mainTitle, h1')), url: location.href, page_title: document.title};
  out.price = T(document.querySelector('.x-price-primary'));
  out.condition = T(document.querySelector('.x-item-condition-text .ux-textspans, .x-item-condition-value .ux-textspans'));
  out.status_msgs = [...document.querySelectorAll('.d-statusmessage, .ux-message, [class*="status-message"]')].map(T).filter(Boolean).slice(0,6);
  const specs = {};
  const clean = el => { if (!el) return null; const c = el.cloneNode(true); c.querySelectorAll('button, .clipped, script, style').forEach(x => x.remove()); return (c.textContent||'').replace(/\s+/g,' ').trim(); };
  document.querySelectorAll('.ux-layout-section-evo__col, .ux-labels-values--inline, .ux-layout-section__row .ux-labels-values').forEach(col => {
    const k = clean(col.querySelector('.ux-labels-values__labels')); const v = clean(col.querySelector('.ux-labels-values__values'));
    if (k && v && !(k in specs) && !/^(Shipping|Delivery|Returns|Payments)$/i.test(k.replace(/:$/,''))) specs[k.replace(/:$/,'')] = v;
  });
  out.specifics = specs;
  const imgs = new Set();
  document.querySelectorAll('.ux-image-carousel-item img, .ux-image-carousel img').forEach(im => {
    const u = im.getAttribute('data-zoom-src') || im.getAttribute('data-src') || im.currentSrc || im.src;
    if (u && u.includes('ebayimg.com')) imgs.add(u);
  });
  out.images = [...imgs];
  const ifr = document.querySelector('#desc_ifr'); out.desc_src = ifr ? ifr.getAttribute('src') : null;
  out.body_flags = {sold: /This listing sold|This item sold|Sold on/i.test(document.body.innerText.slice(0,20000)), ended: /listing (was|has) ended|This listing ended/i.test(document.body.innerText.slice(0,20000))};
  return out;
})()
