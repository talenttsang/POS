/* Pikumi POS register. Plain JS, no build step.

   The browser holds the cart; the server prices every line from the database
   and checks the totals again before writing a sale. Each sale carries a
   clientRecordId generated here, so a retried send after a dropped connection
   can never record the same sale twice. */

(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // The payment screen (cash received, quick amounts, keypad, change) is
  // switched off for now: Charge records the sale at the exact total straight
  // away. Set this to true to bring the screen back; its code is all still here.
  const PAYMENT_SCREEN = true;   // cash goes through the tender screen: cash received, change due

  const state = {
    config: null,
    eventId: null,                // the event this device sells under
    category: 'all',
    products: [],
    cart: [],                     // [{ product, qty, barcode }]
    discount: 0,
    discountMode: localStorage.getItem('pos.discountMode') === 'percent' ? 'percent' : 'amount',
    taxOff: false,                // the venue's tax switched off for this one sale
    cashCents: 0,
    pendingSale: null,            // the payload being sent, kept for retry
    orders: [],
    selectedOrder: null,
  };

  // ------------------------------------------------------------ utilities

  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data = null;
    try { data = await response.json(); } catch { /* no body */ }
    if (response.status === 401) { showLock(); throw new Error('Till is locked'); }
    if (!response.ok) throw new Error((data && data.error) || `Request failed (${response.status})`);
    return data;
  };

  const currentEvent = () => (state.config?.events || []).find((e) => e.id === state.eventId) || null;

  const money = (value) => {
    const n = Number(value || 0);
    const cur = currentEvent()?.currency || 'CAD';
    try {
      return new Intl.NumberFormat('en-CA', { style: 'currency', currency: cur }).format(n);
    } catch { return `${cur} ${n.toFixed(2)}`; }
  };
  const cents = (n) => Math.round(n * 100) / 100;
  const initials = (name) => name.trim().split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase() || '?';

  let toastTimer;
  const toast = (msg) => {
    const el = $('toast');
    el.textContent = msg;
    el.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('on'), 2600);
  };

  const recordId = () => (crypto.randomUUID ? crypto.randomUUID() : `r${Date.now()}${Math.random().toString(36).slice(2)}`);

  // ------------------------------------------------------------ views

  const go = (view) => {
    document.querySelectorAll('.view').forEach((v) => v.classList.toggle('on', v.id === `view-${view}`));
    document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('on', b.dataset.view === view || (view === 'pay' && b.dataset.view === 'sell') || (view === 'done' && b.dataset.view === 'sell')));
    $('pageTitle').textContent = { sell: 'Sell', pay: 'Payment', done: 'Sale complete', orders: 'Sales', analytics: 'Analytics' }[view] || 'Sell';
    if (view === 'sell') setTimeout(() => $('searchInput').focus(), 50);
    if (view === 'orders') loadOrders();
    if (view === 'analytics') loadAnalytics();
    // The Sales list keeps itself current while it is on screen (other tills, refunds, card
    // outcomes), without anyone tapping Refresh. Nothing polls while the tab is hidden.
    clearInterval(ordersTimer);
    if (view === 'orders') ordersTimer = setInterval(() => { if (!document.hidden && !document.querySelector('.sheet.on')) loadOrders(); }, 20000);
  };
  let ordersTimer = null;

  const openSheet = (id) => { $('scrim').classList.add('on'); $(id).classList.add('on'); };
  const closeSheets = () => { $('scrim').classList.remove('on'); document.querySelectorAll('.sheet').forEach((s) => s.classList.remove('on')); };

  // ------------------------------------------------------------ lock screen

  const showLock = () => { $('lock').hidden = false; $('app').hidden = true; loadNames(); setTimeout(() => $('lockCode').focus(), 50); };
  const showApp = () => { $('lock').hidden = true; $('app').hidden = false; renderUser(); go('sell'); };

  $('lockForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('lockErr').textContent = '';
    $('lockBtn').disabled = true;
    try {
      const staff = $('lockStaff').value.trim();
      const data = await api('/api/unlock', { method: 'POST', body: { pin: $('lockCode').value, staff } });
      state.config = await api('/api/config');
      chooseEvent();
      state.config.unlocked = true;
      state.config.staff = data.staff;
      state.config.role = data.role;
      localStorage.setItem('pos.staff', data.staff);
      $('lockCode').value = '';
      showApp();
      await loadCatalogue();
    } catch (err) {
      $('lockErr').textContent = err.message;
    } finally {
      $('lockBtn').disabled = false;
    }
  });

  $('lockOut').addEventListener('click', async () => {
    await api('/api/lock', { method: 'POST' }).catch(() => {});
    state.config.unlocked = false;
    showLock();
  });

  // ------------------------------------------------------------ user

  const renderUser = () => {
    $('navAnalytics').hidden = state.config?.role !== 'admin';
    const name = state.config?.staff || '';
    $('userName').textContent = name;
    $('userAv').textContent = initials(name);
    renderEventSelect();
  };

  // Which event this device sells under. Remembered per device; falls back to
  // the server's default (the event running today, else the next one).
  const chooseEvent = () => {
    const events = state.config.events || [];
    const saved = localStorage.getItem('pos.event');
    state.eventId = events.some((e) => e.id === saved) ? saved : state.config.defaultEventId;
  };
  const venueWord = () => (state.config?.mode === 'store' ? 'store' : 'event');
  const eventLabel = (e) => `${e.name} · ${e.currency}`;
  const renderEventSelect = () => {
    const events = state.config.events || [];
    const sel = $('eventSelect');
    sel.innerHTML = events.length
      ? events.map((e) => `<option value="${esc(e.id)}"${e.id === state.eventId ? ' selected' : ''}>${esc(eventLabel(e))}</option>`).join('')
      : `<option value="">No ${venueWord()} set up</option>`;
    sel.disabled = events.length < 2;
    sel.title = `${venueWord() === 'store' ? 'Store' : 'Event'} being sold under`;
    const an = $('anEvent');
    an.innerHTML = events.map((e) => `<option value="${esc(e.id)}"${e.id === state.eventId ? ' selected' : ''}>${esc(e.name)}${e.kind === 'store' ? '' : ` (${esc(e.startsOn)} to ${esc(e.endsOn)})`}</option>`).join('');
    const allChip = document.querySelector('#anRanges [data-range="all"]');
    if (allChip) allChip.textContent = venueWord() === 'store' ? 'All time' : 'Whole show';
  };
  $('eventSelect').addEventListener('change', () => {
    state.eventId = $('eventSelect').value;
    localStorage.setItem('pos.event', state.eventId);
    renderCart();
    renderEventSelect();
    toast(`Selling at ${currentEvent()?.name || `no ${venueWord()}`}`);
  });
  const loadNames = async () => {
    try {
      const names = await api('/api/users');
      $('lockNames').innerHTML = names.map((u) => `<button type="button" class="chip" data-name="${esc(u.name)}">${esc(u.name)}</button>`).join('');
      if (!names.length) $('lockHint').textContent = 'No users yet. Open the admin portal with the master code to add the first one.';
    } catch { /* names are a convenience; typing still works */ }
  };
  $('lockNames').addEventListener('click', (e) => {
    const b = e.target.closest('[data-name]');
    if (!b) return;
    $('lockStaff').value = b.dataset.name;
    document.querySelectorAll('#lockNames .chip').forEach((c) => c.classList.toggle('on', c === b));
    $('lockCode').focus();
  });

  // ------------------------------------------------------------ catalogue

  // "In stock" filter: on every time the register opens; a tap shows everything for
  // the moment. Only offered where the venue has a stock feed.
  state.inStock = true;
  try { localStorage.removeItem('pos.inStock'); } catch { /* older builds remembered it per device */ }
  const loadCatalogue = async () => {
    const cats = await api(`/api/categories?${new URLSearchParams({ event: state.eventId || '' })}`);
    const hasStock = cats.some((c) => c.in_stock != null);
    if (!hasStock) state.inStock = false;
    const count = (c) => (state.inStock ? c.in_stock : c.n);
    const total = cats.reduce((a, c) => a + c.n, 0);
    const inStockTotal = cats.reduce((a, c) => a + (c.in_stock || 0), 0);
    const stockChip = hasStock
      ? `<button class="chip stock-chip${state.inStock ? ' on' : ''}" id="inStockChip" aria-pressed="${state.inStock}">In stock <span style="opacity:.6">${inStockTotal}</span></button><span class="chip-sep" aria-hidden="true"></span>`
      : '';
    $('catChips').innerHTML = stockChip + [{ category: 'all', n: total, in_stock: inStockTotal }, ...cats]
      .map((c) => `<button class="chip${state.category === c.category ? ' on' : ''}" data-cat="${esc(c.category)}">${c.category === 'all' ? 'All' : esc(c.category)} <span style="opacity:.6">${count(c)}</span></button>`)
      .join('');
    await searchProducts();
  };

  const renderSearchClear = () => { $('searchClear').hidden = !$('searchInput').value; };
  $('searchClear').addEventListener('click', () => {
    $('searchInput').value = '';
    renderSearchClear();
    clearTimeout(searchTimer);
    $('searchInput').focus({ preventScroll: true });
    searchProducts().catch((err) => toast(err.message));
  });
  $('searchInput').addEventListener('input', renderSearchClear);

  let searchTimer;
  const searchProducts = async () => {
    renderSearchClear();
    const q = $('searchInput').value.trim();
    const params = new URLSearchParams({ q, category: state.category, event: state.eventId || '', inStock: state.inStock ? '1' : '0' });
    state.products = await api(`/api/products?${params}`);
    renderGrid();
  };

  const renderGrid = () => {
    if (!state.products.length) { $('grid').innerHTML = `<div class="empty">${state.inStock ? 'Nothing in stock matches. Tap “In stock” to show everything.' : 'Nothing matches.'}</div>`; return; }
    const canEdit = !!(state.config && state.config.unlocked);
    $('grid').innerHTML = state.products.map((p) => `
      <button class="card" data-id="${esc(p.id)}">
        ${canEdit ? '<span class="edit" role="button" aria-label="Edit product" data-edit><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg></span>' : ''}
        ${thumb(p, 'photo')}
        <div class="name">${esc(p.name)}</div>
        <div class="meta"><span class="tag">${esc(p.category)}</span>${p.isSet ? '<span class="tag">set</span>' : ''}${p.isClearance ? '<span class="tag warn">clearance</span>' : ''}${stockTag(p.stock)}</div>
        <div class="price money">${money(p.price)}</div>
      </button>`).join('');
  };

  // On-hand quantity at this store's SkuVault location; null when the store has none.
  const stockTag = (n) => (n == null ? '' : n <= 0 ? '<span class="tag out">out of stock</span>' : `<span class="tag stock">${n} in stock</span>`);

  // Product photo, or a neutral placeholder with the first letters of the name.
  // A photo that fails to load falls back to the placeholder (onerror).
  const thumb = (p, cls) => {
    const ph = `<div class="${cls} ph" aria-hidden="true">${esc(p.name.replace(/^[A-Za-z]+_/, '').slice(0, 2).toUpperCase())}</div>`;
    if (!p.image) return ph;
    return `<img class="${cls}" src="${esc(p.image)}" alt="" loading="lazy" referrerpolicy="no-referrer"
      onerror="this.outerHTML=${esc(JSON.stringify(ph))}">`;
  };

  $('catChips').addEventListener('click', (e) => {
    if (e.target.closest('#inStockChip')) {
      state.inStock = !state.inStock;
      loadCatalogue().catch((err) => toast(err.message));
      return;
    }
    const b = e.target.closest('[data-cat]');
    if (!b) return;
    state.category = b.dataset.cat;
    document.querySelectorAll('#catChips .chip').forEach((c) => c.classList.toggle('on', c.dataset.cat === state.category));
    searchProducts().catch((err) => toast(err.message));
  });

  $('grid').addEventListener('click', (e) => {
    const b = e.target.closest('[data-id]');
    if (!b) return;
    const p = state.products.find((x) => x.id === b.dataset.id);
    if (!p) return;
    if (e.target.closest('[data-edit]')) { openEdit(p); return; }
    addToCart(p);
  });

  // ---- product edit (any signed-in user): barcode and description go to SkuVault
  let editing = null;
  const openEdit = async (p) => {
    editing = { id: p.id };
    $('editErr').textContent = ''; $('editCodes').textContent = '';
    $('editSku').textContent = p.sku; $('editName').value = p.name; $('editPrice').value = p.price; $('editCategory').value = p.category;
    $('editBarcode').value = ''; $('editSvDesc').value = ''; $('editSave').disabled = true;
    openSheet('editSheet');
    $('editSvNote').textContent = 'Loading…';
    try {
      const d = await api(`/api/products/${encodeURIComponent(p.id)}/detail`);
      if (!editing || editing.id !== p.id) return;
      editing.detail = d;
      $('editName').value = d.name; $('editPrice').value = d.price ?? ''; $('editCategory').value = d.category;
      const sv = d.skuvault || {};
      $('editBarcode').value = sv.code || ''; $('editSvDesc').value = sv.description || '';
      $('editSvDesc').disabled = !sv.found;
      $('editSvNote').textContent = sv.found ? `Brand ${sv.brand || '—'}. A barcode already used by another SKU is refused.`
        : 'This SKU is not in SkuVault: the barcode is saved on the register only.';
      const others = (d.barcodes || []).filter((c) => c !== sv.code);
      $('editCodes').textContent = others.length ? `Other scan codes on the register: ${others.join(', ')}` : '';
      $('editSave').disabled = false;
      setTimeout(() => $('editBarcode').focus({ preventScroll: true }), 50);
    } catch (err) { $('editSvNote').textContent = ''; $('editErr').textContent = err.message; }
  };
  $('editBarcode').addEventListener('keydown', (e) => { if (e.key === 'Enter') e.preventDefault(); });   // a scanner ends with Enter
  $('editSave').addEventListener('click', async () => {
    if (!editing || !editing.detail) return;
    const btn = $('editSave'); btn.disabled = true; btn.textContent = 'Saving…'; $('editErr').textContent = '';
    try {
      const body = { name: $('editName').value, price: $('editPrice').value, category: $('editCategory').value, barcode: $('editBarcode').value.trim() };
      if (!$('editSvDesc').disabled) body.svDescription = $('editSvDesc').value;
      const r = await api(`/api/products/${encodeURIComponent(editing.id)}/edit`, { method: 'POST', body });
      closeSheets(); editing = null;
      toast(r.changed.length ? `Saved${r.skuvault ? ' · SkuVault updated' : ''}` : 'Nothing changed');
      await loadCatalogue();
    } catch (err) { $('editErr').textContent = err.message; }
    finally { btn.disabled = false; btn.textContent = 'Save'; }
  });

  // Typing searches; a scanner sends the code followed by Enter, which we treat
  // as a barcode lookup first and a plain search second.
  $('searchInput').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => searchProducts().catch((err) => toast(err.message)), 180);
  });
  $('searchInput').addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const code = $('searchInput').value.trim();
    if (!code) return;
    clearTimeout(searchTimer);
    await scan(code);
  });

  /** A scanned or typed code: barcode lookup first, then a name/SKU search. */
  const scan = async (code) => {
    try {
      const p = await api(`/api/barcode/${encodeURIComponent(code)}`);
      addToCart(p, code);
      toast(`Added ${p.name}`);
      $('searchInput').value = '';
      await searchProducts();
    } catch (err) {
      if (err.message === 'Till is locked') return;
      $('searchInput').value = code;
      await searchProducts();
      if (state.products.length === 1) {
        addToCart(state.products[0]);
        toast(`Added ${state.products[0].name}`);
        $('searchInput').value = '';
        await searchProducts();
      } else if (!state.products.length) {
        toast(`No product for "${code}"`);
      }
    }
  };

  // The scanner works without tapping the search box. A scanner is a keyboard
  // that types the whole code in a fast burst and ends with Enter, so on the
  // Sell screen any burst of characters arriving while no text field has focus
  // is collected and, on Enter, treated as a scan. A pause longer than 150 ms
  // starts a new code, so stray single keys never combine into one.
  let scanBuffer = '';
  let scanLastKey = 0;
  document.addEventListener('keydown', (e) => {
    if ($('app').hidden || !$('view-sell').classList.contains('on')) return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const now = Date.now();
    if (now - scanLastKey > 150) scanBuffer = '';
    scanLastKey = now;
    if (e.key === 'Enter') {
      const code = scanBuffer.trim();
      scanBuffer = '';
      if (code.length >= 3) { e.preventDefault(); scan(code); }
      return;
    }
    if (e.key.length === 1) { scanBuffer += e.key; e.preventDefault(); }
  });

  // ------------------------------------------------------------ cart

  // Out of stock: a red warning before the item goes in, with a way to add it anyway
  // (the count in SkuVault can lag what is on the shelf).
  let stockGo = null;
  const stockGuard = (product, qty, go) => {
    if (product.stock == null || qty <= product.stock) { go(); return; }
    stockGo = go;
    $('stockWarnName').textContent = product.name;
    $('stockWarnTitle').textContent = product.stock <= 0 ? 'Out of stock' : 'Not enough stock';
    $('stockWarnMsg').textContent = product.stock <= 0 ? 'SkuVault shows none left at this store.'
      : `SkuVault shows only ${product.stock} left: this would make ${qty} in the sale.`;
    openSheet('stockSheet');
  };
  $('stockAdd').addEventListener('click', () => { const go = stockGo; stockGo = null; closeSheets(); if (go) go(); });

  // ---- custom item: a name and a price typed at the till, no catalogue product or stock
  const openCustom = () => {
    $('customName').value = ''; $('customPrice').value = ''; $('customQty').value = '1'; $('customErr').textContent = '';
    openSheet('customSheet');
    setTimeout(() => $('customPrice').focus({ preventScroll: true }), 50);
  };
  $('customBtn').addEventListener('click', openCustom);
  $('customAdd').addEventListener('click', () => {
    const price = Math.round(Number(String($('customPrice').value).replace(/[$,\s]/g, '')) * 100) / 100;
    const qty = Math.floor(Number($('customQty').value || 1));
    if (!(price > 0)) { $('customErr').textContent = 'Enter a price above $0.00'; return; }
    if (price > 10000) { $('customErr').textContent = 'A custom item can be at most $10,000'; return; }
    if (!(qty >= 1 && qty <= 999)) { $('customErr').textContent = 'Quantity is 1 to 999'; return; }
    const name = $('customName').value.replace(/\s+/g, ' ').trim().slice(0, 80) || 'Custom item';
    state.cart.push({ product: { id: `custom-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, custom: true, sku: 'CUSTOM', name, price: price.toFixed(2), category: 'Custom', stock: null }, qty, barcode: null });
    closeSheets(); renderCart();
  });
  ['customName', 'customPrice', 'customQty'].forEach((id) => $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); $('customAdd').click(); } }));

  const addToCart = (product, barcode) => {
    const line = state.cart.find((l) => l.product.id === product.id);
    stockGuard(product, (line ? line.qty : 0) + 1, () => {
      if (line) line.qty += 1;
      else state.cart.push({ product, qty: 1, barcode: barcode || null });
      renderCart();
    });
  };

  // A line's discount: a dollar amount or a percentage of the line, never more than the line.
  const lineGross = (l) => cents(Number(l.product.price) * l.qty);
  const lineDiscount = (l) => {
    if (l.gift) return lineGross(l);                 // a gift is the whole line off: $0
    if (!l.disc || !(l.disc.value > 0)) return 0;
    const raw = l.disc.mode === 'percent' ? lineGross(l) * Math.min(l.disc.value, 100) / 100 : l.disc.value;
    return Math.min(cents(raw), lineGross(l));
  };
  const lineItems = () => state.cart.map((l) => ({ ...(l.product.custom ? { custom: true, name: l.product.name, unitPrice: l.product.price } : { productId: l.product.id }), quantity: l.qty, barcode: l.barcode,
    ...(l.gift ? { gift: true } : l.disc && l.disc.value > 0 ? (l.disc.mode === 'percent' ? { discountPercent: Math.min(l.disc.value, 100) } : { discount: lineDiscount(l) }) : {}) }));

  const cartTotals = () => {
    const subtotal = cents(state.cart.reduce((a, l) => a + lineGross(l) - lineDiscount(l), 0));
    const entered = Math.max(0, state.discount || 0);
    const percent = state.discountMode === 'percent' ? Math.min(entered, 100) : null;
    const discount = Math.min(cents(percent === null ? entered : subtotal * percent / 100), subtotal);
    // The venue's sales tax goes on top of what is left after the discount; the server recomputes it.
    const rate = state.taxOff ? 0 : Number(currentEvent()?.taxRate || 0);
    const tax = cents((subtotal - discount) * rate);
    const total = cents(subtotal - discount + tax);
    return { subtotal, discount, tax, taxLabel: currentEvent()?.taxLabel || 'Tax', total, percent };
  };

  const totalsHtml = (t) => `
    <div class="trow"><span>Subtotal</span><span class="money">${money(t.subtotal)}</span></div>
    ${t.discount ? `<div class="trow"><span>Discount${t.percent ? ` (${t.percent}%)` : ''}</span><span class="money">−${money(t.discount)}</span></div>` : ''}
    ${t.tax ? `<div class="trow"><span>${esc(t.taxLabel)}</span><span class="money">${money(t.tax)}</span></div>` : ''}
    <div class="trow grand"><span>Total</span><span class="money">${money(t.total)}</span></div>`;

  // ---- sales tax on/off for the current sale (tax-exempt customers)
  const renderTaxToggle = () => {
    const ev = currentEvent(); const rate = Number(ev?.taxRate || 0);
    $('taxTools').hidden = !rate;
    if (!rate) { state.taxOff = false; return; }
    const label = ev.taxLabel || `${+(rate * 100).toFixed(2)}% tax`;
    $('taxToggle').textContent = state.taxOff ? `${label} off` : `${label} on`;
    $('taxToggle').classList.toggle('on', !state.taxOff);
    $('taxToggle').setAttribute('aria-pressed', String(!state.taxOff));
  };
  const renderCart = () => {
    renderTaxToggle();
    if (!PAYMENT_SCREEN) state.pendingSale = null;   // the cart changed; a retry must send the new cart
    const count = state.cart.reduce((a, l) => a + l.qty, 0);
    $('cartCount').textContent = `${count} item${count === 1 ? '' : 's'}`;
    $('cartLines').innerHTML = state.cart.length ? state.cart.map((l) => `
      <div class="line" data-id="${esc(l.product.id)}">
        <div class="line-main">${thumb(l.product, 'mini')}<div><div class="n">${esc(l.product.name)}</div><div class="s">${esc(l.product.category)} · ${money(l.product.price)} each${l.product.stock != null && l.qty > l.product.stock ? ` · <b class="short">only ${l.product.stock} in stock</b>` : ''}</div></div></div>
        <div class="amt money">${lineDiscount(l) ? `<s>${money(lineGross(l))}</s>` : ''}${money(lineGross(l) - lineDiscount(l))}</div>
        <div class="qty">
          <button data-step="-1" aria-label="Less">−</button><span>${l.qty}</span><button data-step="1" aria-label="More">+</button>
          <button class="rm" data-remove>Remove</button>
          <button class="gift${l.gift ? ' on' : ''}" data-gift aria-pressed="${!!l.gift}">${l.gift ? '✓ Gift' : 'Gift'}</button>
          ${l.gift ? '' : `<button class="disc${lineDiscount(l) ? ' on' : ''}" data-discount>${lineDiscount(l) ? (l.disc.mode === 'percent' ? `−${l.disc.value}%` : `−${money(lineDiscount(l))}`) : 'Discount'}</button>`}
        </div>
      </div>`).join('') : '<div class="empty">Tap a product or scan a barcode.</div>';
    const t = cartTotals();
    $('cartTotals').innerHTML = totalsHtml(t);
    $('chargeBtn').disabled = !state.cart.length;
    const flex = !!(currentEvent() && currentEvent().cloverStore);
    $('cardBtn').hidden = !flex;
    $('cardBtn').disabled = !state.cart.length || cartTotals().total <= 0;
    if (flex) { $('cardBtn').textContent = state.cart.length ? `Card ${money(cartTotals().total)}` : 'Card'; }
    $('chargeBtn').textContent = state.cart.length ? `${currentEvent() && currentEvent().cloverStore ? 'Cash' : 'Charge'} ${money(t.total)}` : 'Charge';
  };

  $('cartLines').addEventListener('click', (e) => {
    const lineEl = e.target.closest('.line');
    if (!lineEl) return;
    const line = state.cart.find((l) => l.product.id === lineEl.dataset.id);
    if (!line) return;
    if (e.target.closest('[data-discount]')) { openLineDiscount(line); return; }
    if (e.target.closest('[data-gift]')) { line.gift = !line.gift; renderCart(); return; }
    if (e.target.closest('[data-remove]')) state.cart = state.cart.filter((l) => l !== line);
    else if (e.target.closest('[data-step]')) {
      const step = Number(e.target.closest('[data-step]').dataset.step);
      if (step > 0) { stockGuard(line.product, line.qty + step, () => { line.qty += step; renderCart(); }); return; }
      line.qty += step;
      if (line.qty <= 0) state.cart = state.cart.filter((l) => l !== line);
    }
    renderCart();
  });
  let discLine = null;
  const openLineDiscount = (line) => {
    discLine = line;
    const mode = (line.disc && line.disc.mode) || state.discountMode;
    $('lineSheetName').textContent = `${line.qty}× ${line.product.name} · ${money(lineGross(line))}`;
    $('lineDiscMode').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
    $('lineDiscInput').value = line.disc && line.disc.value > 0 ? String(line.disc.value) : '';
    $('lineDiscInput').placeholder = mode === 'percent' ? '0' : '0.00';
    $('lineDiscErr').textContent = '';
    openSheet('lineSheet');
  };
  $('lineDiscMode').addEventListener('click', (e) => {
    const b = e.target.closest('[data-mode]'); if (!b) return;
    $('lineDiscMode').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    $('lineDiscInput').placeholder = b.dataset.mode === 'percent' ? '0' : '0.00';
  });
  $('lineDiscApply').addEventListener('click', () => {
    if (!discLine) return;
    const mode = $('lineDiscMode').querySelector('.on').dataset.mode;
    const value = Number($('lineDiscInput').value) || 0;
    if (value < 0 || (mode === 'percent' && value > 100) || (mode === 'amount' && value > lineGross(discLine) + 1e-9)) { $('lineDiscErr').textContent = 'That is more than the line is worth.'; return; }
    discLine.disc = value > 0 ? { mode, value } : null;
    closeSheets(); renderCart();
  });
  $('lineDiscClear').addEventListener('click', () => { if (discLine) discLine.disc = null; closeSheets(); renderCart(); });

  $('clearBtn').addEventListener('click', () => { state.cart = []; state.discount = 0; state.pendingSale = null; state.taxOff = false; $('discountInput').value = ''; renderTaxToggle(); renderCart(); });
  $('discountInput').addEventListener('input', () => { state.discount = Number($('discountInput').value) || 0; renderCart(); });
  const applyDiscountMode = () => {
    const pct = state.discountMode === 'percent';
    $('discountMode').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.mode === state.discountMode));
    $('discountInput').placeholder = pct ? '0' : '0.00';
    renderCart();
  };
  $('discountMode').addEventListener('click', (e) => {
    const b = e.target.closest('[data-mode]');
    if (!b || b.dataset.mode === state.discountMode) return;
    state.discountMode = b.dataset.mode;
    localStorage.setItem('pos.discountMode', state.discountMode);
    state.discount = 0; $('discountInput').value = '';
    applyDiscountMode();
  });
  applyDiscountMode();
  $('taxToggle').addEventListener('click', () => { state.taxOff = !state.taxOff; renderTaxToggle(); renderCart(); });

  // ------------------------------------------------------------ payment

  $('chargeBtn').addEventListener('click', () => (PAYMENT_SCREEN ? openPayment() : chargeExact()));
  $('cardBtn').addEventListener('click', () => chargeCard());

  // ---- card payments on the Clover Flex: start, then poll until the reader answers
  let cardPoll = null;
  const cardPayload = () => ({
    clientRecordId: recordId(),
    eventId: state.eventId,
    items: lineItems(),
    discount: cartTotals().discount,
    taxExempt: state.taxOff,
  });
  const cardUi = (text, { err = '', retry = false, check = false, cancel = 'Cancel' } = {}) => {
    $('cardStatus').textContent = text; $('cardErr').textContent = err;
    $('cardRetry').hidden = !retry; $('cardCheck').hidden = !check; $('cardCancel').textContent = cancel;
  };
  const chargeCard = async (payload) => {
    if (!state.cart.length) return;
    payload = payload || cardPayload();
    state.cardPending = payload;
    $('cardAmount').textContent = money(cartTotals().total);
    cardUi('Sending to the Clover Flex…');
    openSheet('cardSheet');
    try {
      await api('/api/card/charge', { method: 'POST', body: payload });
      cardUi('Hand the Flex to the customer and follow the prompts on its screen.');
      pollCard(payload.clientRecordId);
    } catch (err) {
      cardUi('The charge could not be started.', { err: err.message, retry: true });
    }
  };
  const pollCard = (id) => {
    clearInterval(cardPoll);
    cardPoll = setInterval(async () => {
      try {
        const r = await api(`/api/card/${encodeURIComponent(id)}`);
        if (r.status === 'started') return;
        clearInterval(cardPoll);
        cardOutcome(r);
      } catch (err) { /* keep polling; the network may be flaky */ }
    }, 2000);
  };
  const cardOutcome = (r) => {
    if (r.status === 'paid') {
      closeSheets(); state.cardPending = null;
      finishSale(r.sale);
    } else if (r.status === 'failed') {
      cardUi('The card was not charged.', { err: r.message || 'Declined or cancelled on the reader.', retry: true });
    } else {
      cardUi('No answer from the Flex. The customer may or may not have paid.', { err: r.message || '', check: true, cancel: 'Give up' });
    }
  };
  $('cardRetry').addEventListener('click', () => chargeCard());            // a fresh attempt gets a new record id
  $('cardCheck').addEventListener('click', async () => {
    const id = state.cardPending && state.cardPending.clientRecordId; if (!id) return;
    cardUi('Asking Clover…');
    try { cardOutcome(await api(`/api/card/${encodeURIComponent(id)}/check`, { method: 'POST', body: {} })); }
    catch (err) { cardUi('Could not reach Clover.', { err: err.message, check: true, cancel: 'Give up' }); }
  });
  $('cardCancel').addEventListener('click', () => { clearInterval(cardPoll); closeSheets(); });

  /** Record the sale at the exact total, no payment screen. Re-pressing after a
      failure resends the same payload, so a retry can never double-record. */
  const chargeExact = async () => {
    if (!state.cart.length) return;
    const payload = state.pendingSale || {
      clientRecordId: recordId(),
      eventId: state.eventId,
      items: lineItems(),
      discount: cartTotals().discount,
      taxExempt: state.taxOff,
      cashReceived: cartTotals().total,
    };
    state.pendingSale = payload;
    const btn = $('chargeBtn');
    btn.disabled = true;
    btn.textContent = 'Recording sale…';
    try {
      const sale = await api('/api/sales', { method: 'POST', body: payload });
      state.pendingSale = null;
      finishSale(sale);
    } catch (err) {
      toast(`${err.message}. Check the connection and tap Charge again.`);
      btn.disabled = false;
      btn.textContent = 'Retry charge';
    }
  };
  $('payBack').addEventListener('click', () => go('sell'));

  const openPayment = () => {
    const t = cartTotals();
    state.cashCents = 0;
    $('payAmount').textContent = money(t.total);
    $('payLines').innerHTML = state.cart.map((l) => `<div class="ps-line"><span>${l.qty}× ${esc(l.product.name)}</span><span class="money">${money(Number(l.product.price) * l.qty)}</span></div>`).join('');
    $('payTotals').innerHTML = totalsHtml(t);
    const amounts = [t.total, Math.ceil(t.total / 5) * 5, Math.ceil(t.total / 20) * 20, Math.ceil(t.total / 50) * 50, Math.ceil(t.total / 100) * 100]
      .filter((v, i, a) => v > 0 && a.indexOf(v) === i).slice(0, 4);
    $('cashQuick').innerHTML = amounts.map((v, i) => `<button class="qbtn money" data-cash="${Math.round(v * 100)}">${i === 0 ? 'Exact ' : ''}${money(v)}</button>`).join('');
    $('payErr').textContent = '';
    renderCash();
    go('pay');
  };

  $('cashKeys').innerHTML = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '00', '0', '⌫'].map((k) => `<button class="key" data-key="${k}">${k}</button>`).join('');
  $('cashKeys').addEventListener('click', (e) => {
    const k = e.target.closest('[data-key]')?.dataset.key;
    if (!k) return;
    if (k === '⌫') state.cashCents = Math.floor(state.cashCents / 10);
    else if (k === '00') state.cashCents *= 100;
    else state.cashCents = state.cashCents * 10 + Number(k);
    if (state.cashCents > 99999999) state.cashCents = 99999999;
    renderCash();
  });
  $('cashQuick').addEventListener('click', (e) => {
    const b = e.target.closest('[data-cash]');
    if (!b) return;
    state.cashCents = Number(b.dataset.cash);
    renderCash();
  });

  const renderCash = () => {
    const t = cartTotals();
    const cash = state.cashCents / 100;
    $('cashVal').textContent = money(cash);
    const ok = cash >= t.total - 1e-9;
    $('cashTender').disabled = !ok || !!state.pendingSale;
    $('cashTender').textContent = ok ? `Tender cash · change ${money(cents(cash - t.total))}` : 'Tender cash';
  };

  $('cashTender').addEventListener('click', async () => {
    if (state.pendingSale) return;
    const t = cartTotals();
    const payload = {
      clientRecordId: recordId(),
      eventId: state.eventId,
      items: lineItems(),
      discount: t.discount,
      taxExempt: state.taxOff,
      cashReceived: state.cashCents / 100,
    };
    await sendSale(payload);
  });

  const sendSale = async (payload) => {
    state.pendingSale = payload;
    $('cashTender').disabled = true;
    $('cashTender').textContent = 'Recording sale…';
    $('payErr').textContent = '';
    try {
      const sale = await api('/api/sales', { method: 'POST', body: payload });
      state.pendingSale = null;
      finishSale(sale);
    } catch (err) {
      // Keep the same clientRecordId: the retry cannot double-record.
      $('payErr').textContent = `${err.message}. Check the connection and tap again to retry.`;
      $('cashTender').disabled = false;
      $('cashTender').textContent = 'Retry tender';
      $('cashTender').onclick = () => sendSale(payload);
    }
  };

  const invoiceUrl = (number, to) => `/invoice/${number}.pdf${to ? `?${new URLSearchParams({ to })}` : ''}`;

  const finishSale = (sale) => {
    $('cashTender').onclick = null;
    const change = Number(sale.change);
    $('doneKicker').textContent = change > 0.004 ? 'Change due' : `Sale recorded · ${sale.paymentMethod === 'card' ? 'card' : 'cash'}`;
    $('doneAmount').textContent = money(change > 0.004 ? change : sale.total);
    $('doneMeta').textContent = `Sale #${sale.number} · ${money(sale.total)}${sale.paymentMethod === 'cash' && Number(sale.cashReceived) ? ` · cash received ${money(sale.cashReceived)} · change ${money(sale.change)}` : ''} · ${sale.staff} · ${currentEvent()?.name || ''}`;
    $('doneInvoice').href = invoiceUrl(sale.number);
    state.cart = [];
    state.discount = 0;
    state.taxOff = false;
    $('discountInput').value = '';
    renderTaxToggle();
    renderCart();
    go('done');
    searchProducts().catch(() => {});
  };
  $('newSaleBtn').addEventListener('click', () => go('sell'));

  // ------------------------------------------------------------ orders

  const loadOrders = async () => {
    try {
      const q = $('orderSearch').value.trim();
      const ev = state.eventId || '';
      const [orders, summary] = await Promise.all([
        api(`/api/sales?${new URLSearchParams({ q, limit: 100, event: ev, method: state.orderMethod || '' })}`),
        api(`/api/summary?${new URLSearchParams({ event: ev })}`),
      ]);
      state.orders = orders;
      renderOrders();
      renderSummary(summary);
    } catch (err) { toast(err.message); }
  };

  const payTag = (o) => `<span class="pay pm-${o.paymentMethod === 'card' ? 'card' : 'cash'}">${o.paymentMethod === 'card' ? 'Card' : 'Cash'}</span>`
    + (o.refundMethod && o.refundMethod !== o.paymentMethod ? ` <span class="pay pm-${o.refundMethod}">refunded ${o.refundMethod === 'card' ? 'to card' : 'in cash'}</span>` : '');
  $('orderMethod').addEventListener('click', (e) => {
    const b = e.target.closest('[data-method]'); if (!b) return;
    state.orderMethod = b.dataset.method;
    document.querySelectorAll('#orderMethod .chip').forEach((c) => c.classList.toggle('on', c === b));
    loadOrders();
  });
  const statusOf = (o) => (o.status === 'refunded' ? ['refunded', 'Refunded'] : o.refundAmount ? ['partial', 'Part refunded'] : ['completed', 'Paid']);

  const renderOrders = () => {
    $('orderList').innerHTML = state.orders.length ? state.orders.map((o) => {
      const [cls, label] = statusOf(o);
      const when = new Date(o.soldAt).toLocaleTimeString('en-CA', { hour: 'numeric', minute: '2-digit' });
      return `<button class="order${state.selectedOrder === o.number ? ' on' : ''}" data-num="${o.number}">
        <div class="num">#${o.number}</div>
        <div><div>${o.items.length} line${o.items.length === 1 ? '' : 's'} · ${esc(o.staff)}</div><div class="who">${when} · ${payTag(o)} <span class="status ${cls}">${label}</span></div></div>
        <div class="tot money">${money(o.total)}</div>
      </button>`;
    }).join('') : '<div class="empty">No sales yet.</div>';
  };

  const renderSummary = (rows) => {
    if (!rows.length) { $('summaryTable').innerHTML = ''; return; }
    // Day total first, then how it splits: cash (count the drawer) and card (match Clover).
    const cell = (net, n, refunded) => `<b class="money">${money(net)}</b><div class="sub">${n} sale${n === 1 ? '' : 's'}${Number(refunded) ? ` · ${money(refunded)} refunded` : ''}</div>`;
    $('summaryTable').innerHTML = `<tr><th>Day</th><th class="tot">Total</th><th class="grp pm-cash">Cash · count the drawer</th><th class="grp pm-card">Card · match Clover</th></tr>`
      + rows.map((r) => {
        const net = num(r.cash_net) + num(r.card_net);
        const refunded = num(r.cash_refunded) + num(r.card_refunded);
        return `<tr><td>${esc(r.day)}</td><td class="tot">${cell(net, r.cash_sales + r.card_sales, refunded)}</td>
          <td>${cell(r.cash_net, r.cash_sales, r.cash_refunded)}</td><td>${cell(r.card_net, r.card_sales, r.card_refunded)}</td></tr>`;
      }).join('');
  };

  $('orderList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-num]');
    if (!b) return;
    state.selectedOrder = Number(b.dataset.num);
    renderOrders();
    renderDetail();
  });
  $('refreshOrders').addEventListener('click', loadOrders);
  let orderTimer;
  $('orderSearch').addEventListener('input', () => { clearTimeout(orderTimer); orderTimer = setTimeout(loadOrders, 250); });

  const renderDetail = () => {
    const o = state.orders.find((x) => x.number === state.selectedOrder);
    if (!o) { $('orderDetail').innerHTML = '<div class="muted">Pick a sale to see its lines.</div>'; return; }
    const [, label] = statusOf(o);
    $('orderDetail').innerHTML = `
      <h3>Sale #${o.number}</h3>
      <div class="muted">${new Date(o.soldAt).toLocaleString('en-CA')} · ${esc(o.staff)} · ${label}</div>
      <div>${payTag(o)}</div>
      <div>${o.items.map((i) => `<div class="ps-line"><span>${i.quantity}× ${esc(i.name)}${i.gift ? ' <span class="pay gift-tag">Gift</span>' : Number(i.lineDiscount) ? ` <span class="muted">(−${money(i.lineDiscount)})</span>` : ''}</span><span class="money">${money(i.lineTotal)}</span></div>`).join('')}</div>
      <div class="totals" style="padding:0">
        <div class="trow"><span>Subtotal</span><span class="money">${money(o.subtotal)}</span></div>
        ${Number(o.discount) ? `<div class="trow"><span>Discount</span><span class="money">−${money(o.discount)}</span></div>` : ''}
        ${Number(o.tax) ? `<div class="trow"><span>${esc(o.taxLabel || 'Tax')}</span><span class="money">${money(o.tax)}</span></div>` : ''}
        <div class="trow grand"><span>Total</span><span class="money">${money(o.total)}</span></div>
        ${o.paymentMethod === 'card' ? '<div class="trow"><span>Paid by card</span><span>Clover Flex</span></div>' : `<div class="trow"><span>Cash received</span><span class="money">${money(o.cashReceived)}</span></div>
        <div class="trow"><span>Change</span><span class="money">${money(o.change)}</span></div>`}
        ${o.refundAmount ? `<div class="trow" style="color:var(--danger)"><span>Refunded · ${esc(o.refundReason || '')}</span><span class="money">−${money(o.refundAmount)}</span></div>` : ''}
      </div>
      <div class="invoice-row">
        <input id="invoiceTo" placeholder="Customer name on invoice (optional)" autocomplete="off" autocapitalize="words" aria-label="Customer name on invoice">
        <a class="btn" id="invoiceBtn" href="${invoiceUrl(o.number)}" target="_blank" rel="noopener">Invoice</a>
      </div>
      ${o.refundAmount ? '' : `<button class="btn danger" id="refundBtn">Refund</button>`}`;
    $('invoiceTo').addEventListener('input', () => { $('invoiceBtn').href = invoiceUrl(o.number, $('invoiceTo').value.trim()); });
    const rb = $('refundBtn');
    if (rb) rb.addEventListener('click', () => {
      $('refundNum').textContent = `#${o.number}`;
      $('refundReason').value = ''; $('refundAmount').value = ''; $('refundCode').value = ''; $('refundAdminName').value = ''; $('refundAdminPin').value = ''; $('refundErr').textContent = '';
      $('refundApproval').hidden = state.config.role === 'admin';
      $('refundCash').hidden = true;
      $('refundHow').textContent = o.paymentMethod === 'card' ? 'The money goes back to the customer\'s card through Clover; an admin has to approve.' : 'Refunds are paid back in cash and need an admin to approve.';
      $('refundAmount').placeholder = `Amount (blank = full ${money(o.amountPaid)})`;
      openSheet('refundSheet');
    });
  };

  const sendRefund = async (method) => {
    const o = state.orders.find((x) => x.number === state.selectedOrder);
    if (!o) return;
    const body = { reason: $('refundReason').value, code: $('refundCode').value, adminName: $('refundAdminName').value, adminPin: $('refundAdminPin').value };
    if ($('refundAmount').value) body.amount = Number($('refundAmount').value);
    if (method) body.method = method;
    if (!body.reason) { $('refundErr').textContent = 'Pick a reason'; return; }
    const btn = $('refundConfirm'); const cashBtn = $('refundCash');
    btn.disabled = cashBtn.disabled = true;
    btn.textContent = o.paymentMethod === 'card' && method !== 'cash' ? 'Refunding on the Flex…' : 'Refunding…';
    try {
      await api(`/api/sales/${o.number}/refund`, { method: 'POST', body });
      closeSheets();
      toast(`Refunded sale #${o.number}${method === 'cash' && o.paymentMethod === 'card' ? ' in cash' : ''}`);
    } catch (err) {
      // A refund that already went through (a second tap, or a reply lost on the way) is done, not an error.
      if (/already refunded/i.test(err.message)) { closeSheets(); toast(`Sale #${o.number} is already refunded`); }
      else {
        $('refundErr').textContent = err.message;
        // Clover would not take the refund back to the card: let the admin hand cash over instead.
        if (o.paymentMethod === 'card' && !method) cashBtn.hidden = false;
      }
    } finally {
      btn.disabled = cashBtn.disabled = false; btn.textContent = 'Refund';
      await loadOrders().catch(() => {});
      renderDetail();
    }
  };
  $('refundConfirm').addEventListener('click', () => sendRefund());
  $('refundCash').addEventListener('click', () => sendRefund('cash'));

  // ------------------------------------------------------------ analytics (admins)

  const an = { range: 'all', from: '', to: '', data: null };
  const num = (v) => Number(v || 0);
  const money0 = (v) => { const n = num(v); return Math.abs(n) >= 10000 ? money(n).replace(/\.\d\d$/, '') : money(n); };
  const pct = (v) => (v == null ? '—' : `${Number(v).toFixed(v >= 10 ? 0 : 1)}%`);
  const localDate = (d) => { const z = new Date(d.getTime() - d.getTimezoneOffset() * 60000); return z.toISOString().slice(0, 10); };

  const rangeDates = () => {
    const today = new Date();
    const day = (offset) => { const d = new Date(today); d.setDate(d.getDate() + offset); return localDate(d); };
    switch (an.range) {
      case 'today': return [day(0), day(0)];
      case 'yesterday': return [day(-1), day(-1)];
      case '7d': return [day(-6), day(0)];
      case 'custom': return [an.from, an.to];
      default: return ['', ''];
    }
  };

  const loadAnalytics = async () => {
    const [from, to] = rangeDates();
    const params = new URLSearchParams(); if (from) params.set('from', from); if (to) params.set('to', to);
    params.set('event', $('anEvent').value || state.eventId || '');
    $('anExport').href = `/api/analytics/export.csv${params.toString() ? '?' + params : ''}`;
    try {
      an.data = await api(`/api/analytics?${params}`);
      renderAnalytics();
    } catch (err) { toast(err.message); }
  };

  $('anRanges').addEventListener('click', (e) => {
    const b = e.target.closest('[data-range]'); if (!b) return;
    an.range = b.dataset.range; $('anFrom').value = ''; $('anTo').value = '';
    document.querySelectorAll('#anRanges .chip').forEach((c) => c.classList.toggle('on', c === b));
    loadAnalytics();
  });
  $('anApply').addEventListener('click', () => {
    an.from = $('anFrom').value; an.to = $('anTo').value;
    if (!an.from && !an.to) { toast('Pick a from or to date'); return; }
    an.range = 'custom';
    document.querySelectorAll('#anRanges .chip').forEach((c) => c.classList.remove('on'));
    loadAnalytics();
  });
  $('anRefresh').addEventListener('click', loadAnalytics);
  $('anEvent').addEventListener('change', loadAnalytics);

  const kpiTile = (label, value, detail, hero) => `<div class="kpi"><div class="l">${esc(label)}</div><div class="v${hero ? ' hero' : ''} money">${esc(value)}</div>${detail ? `<div class="d">${esc(detail)}</div>` : ''}</div>`;

  const renderAnalytics = () => {
    const d = an.data; const k = d.kpi;
    const r = d.range;
    const ev = d.event;
    $('anRangeNote').textContent = (r.from || r.to)
      ? `${ev.name} · showing ${r.from || 'the start'} to ${r.to || 'today'} · ${k.sales} sales`
      : ev.kind === 'store'
        ? `${ev.name} · all time · ${k.sales} sales`
        : `${ev.name} · ${ev.startsOn} to ${ev.endsOn} · whole event · ${k.sales} sales`;
    const coverage = k.costCoveragePct == null ? '' : `cost known for ${pct(k.costCoveragePct)} of revenue`;
    $('anKpis').innerHTML = [
      kpiTile('Net revenue', money0(k.netRevenue), `${money0(k.grossRevenue)} sold, ${money0(k.refunds)} refunded`, true),
      kpiTile('Gross margin', k.grossMargin == null ? '—' : money0(k.grossMargin), k.grossMarginPct == null ? 'no product costs yet' : `${pct(k.grossMarginPct)} · ${coverage}`),
      kpiTile('Sales', String(k.sales), `${k.units} units · ${k.refundedSales} refunded`),
      kpiTile('Average sale', k.avgBasket == null ? '—' : money(k.avgBasket), k.sales ? `${(k.units / k.sales).toFixed(1)} units per sale` : ''),
      kpiTile('Discounts given', money0(k.discounts), k.grossRevenue > 0 ? `${pct(num(k.discounts) / (num(k.grossRevenue) + num(k.discounts)) * 100)} of list` : ''),
    ].join('');

    barChart($('figDay'), d.byDay.map((x) => ({ label: x.day.slice(5), value: num(x.revenue) - num(x.refunds), tip: `${x.day}: ${money(num(x.revenue) - num(x.refunds))} net · ${x.sales} sales · ${x.units} units` })), { columns: true });
    const hours = Array.from({ length: 24 }, (_, h) => ({ h, sales: 0, revenue: 0 }));
    d.byHour.forEach((x) => { hours[x.hour] = { h: x.hour, sales: x.sales, revenue: num(x.revenue) }; });
    const active = hours.filter((x, i, a) => x.sales || (a.slice(0, i).some((y) => y.sales) && a.slice(i).some((y) => y.sales)));
    barChart($('figHour'), active.map((x) => ({ label: `${x.h}h`, value: x.revenue, tip: `${x.h}:00–${x.h}:59: ${money(x.revenue)} · ${x.sales} sales` })), { columns: true, labelEvery: active.length > 12 ? 2 : 1 });
    barChart($('figCategory'), d.byCategory.map((x) => ({ label: x.category, value: num(x.revenue), tip: `${x.category}: ${money(num(x.revenue))} · ${x.units} units${x.cost_of_goods != null ? ` · margin ${pct((num(x.revenue_with_cost) - num(x.cost_of_goods)) / num(x.revenue_with_cost) * 100)}` : ''}` })));
    barChart($('figStaff'), d.byStaff.map((x) => ({ label: x.staff, value: num(x.revenue), tip: `${x.staff}: ${money(num(x.revenue))} · ${x.sales} sales · ${money(num(x.refunds))} refunded` })));
    stackChart($('figStock'), d.stock.filter((x) => num(x.brought) > 0).map((x) => ({ label: x.category, sold: num(x.sold), total: num(x.brought), tip: `${x.category}: ${x.sold} of ${x.brought} units sold (${pct(num(x.sold) / num(x.brought) * 100)}) · ${money0(x.remaining_value)} of stock left at price` })));
    barChart($('figTopUnits'), (d.topUnits || []).slice(0, 12).map((x) => ({ label: x.name || x.sku, value: num(x.units), tip: `${x.name || x.sku}: ${x.units} units · ${money(num(x.revenue))}${num(x.gift_units) ? ` · ${x.gift_units} given as gifts` : ''}` })), { labelWidth: 330, fmt: (v) => `${v} unit${v === 1 ? '' : 's'}` });
    barChart($('figTop'), d.topProducts.slice(0, 12).map((x) => ({ label: x.name || x.sku, value: num(x.revenue), tip: `${x.name || x.sku}: ${money(num(x.revenue))} · ${x.units} units${x.margin != null ? ` · margin ${money(num(x.margin))}` : ''}${x.brought ? ` · ${x.units}/${x.brought} brought` : ''}` })), { labelWidth: 330 });

    const pay = Object.fromEntries((d.byPayment || []).map((x) => [x.method, x]));
    $('anPay').innerHTML = ['cash', 'card'].map((m) => { const x = pay[m] || { sales: 0, taken: 0, refunded: 0, net: 0 };
      return `<div class="pay-tile pm-${m}"><div class="l"><span class="pay pm-${m}">${m === 'card' ? 'Card' : 'Cash'}</span> ${m === 'card' ? 'match to Clover' : 'count against the drawer'}</div>
        <div class="v money">${money(num(x.net))}</div><div class="d">${x.sales} sale${Number(x.sales) === 1 ? '' : 's'} · ${money(num(x.taken))} taken · ${money(num(x.refunded))} refunded</div></div>`; }).join('');
    $('tblDay').innerHTML = table(['Day', 'Sales', 'Units', 'Sold', 'Refunds', 'Net', 'Cash net', 'Card net'], d.byDay.map((x) => [x.day, x.sales, x.units, money(num(x.revenue)), money(num(x.refunds)), money(num(x.revenue) - num(x.refunds)), money(num(x.cash_net)), money(num(x.card_net))]));
    $('tblCategory').innerHTML = table(['Category', 'Units', 'Revenue', 'Cost', 'Margin'], d.byCategory.map((x) => [x.category, x.units, money(num(x.revenue)), x.cost_of_goods == null ? '—' : money(num(x.cost_of_goods)), x.cost_of_goods == null ? '—' : pct((num(x.revenue_with_cost) - num(x.cost_of_goods)) / num(x.revenue_with_cost) * 100)]));
  };

  const table = (head, rows) => rows.length
    ? `<table><tr>${head.map((h) => `<th>${esc(h)}</th>`).join('')}</tr>${rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</table>`
    : '<div class="hint">No sales in this range.</div>';

  const attachTips = (fig) => {
    const plot = fig.querySelector('.plot');
    let tip = plot.querySelector('.tip');
    if (!tip) { tip = document.createElement('div'); tip.className = 'tip'; plot.appendChild(tip); }
    plot.querySelectorAll('[data-tip]').forEach((el) => {
      el.addEventListener('mousemove', (e) => { const r = plot.getBoundingClientRect(); tip.textContent = el.dataset.tip; tip.style.left = `${e.clientX - r.left}px`; tip.style.top = `${e.clientY - r.top}px`; tip.classList.add('on'); });
      el.addEventListener('mouseleave', () => tip.classList.remove('on'));
    });
  };

  const nice = (max) => { if (max <= 0) return 1; const p = 10 ** Math.floor(Math.log10(max)); const m = max / p; const step = m <= 1 ? 0.25 : m <= 2 ? 0.5 : m <= 5 ? 1 : 2; return Math.ceil(m / step) * step * p; };

  /** Horizontal bars (default) or columns; single series, sequential hue; hover tooltips; end labels. */
  const barChart = (fig, items, opts = {}) => {
    const plot = fig.querySelector('.plot');
    if (!items.length) { plot.innerHTML = '<div class="empty hint">No sales in this range.</div>'; return; }
    // Full-width figures get a proportionally wider canvas so text renders at the same size.
    const W = fig.classList.contains('fig-wide') ? 1150 : 560; const max = nice(Math.max(...items.map((i) => i.value)));
    let svg = '';
    if (opts.columns) {
      const H = 220, padL = 44, padB = 26, padT = 14; const w = (W - padL) / items.length; const bw = Math.min(24, w * 0.6);
      const y = (v) => padT + (H - padT - padB) * (1 - v / max);
      for (let g = 0; g <= 4; g++) { const v = max * g / 4; svg += `<line class="grid" x1="${padL}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text x="${padL - 6}" y="${y(v) + 3}" text-anchor="end">${money0(v)}</text>`; }
      items.forEach((it, i) => {
        const x = padL + w * i + (w - bw) / 2; const top = y(it.value); const h = Math.max(0, y(0) - top);
        svg += `<rect class="hit" data-tip="${esc(it.tip)}" x="${padL + w * i}" y="${padT}" width="${w}" height="${H - padT - padB}"/>`;
        svg += `<path class="bar" data-tip="${esc(it.tip)}" d="${roundTop(x, top, bw, h)}"/>`;
        if (!opts.labelEvery || i % opts.labelEvery === 0) svg += `<text x="${x + bw / 2}" y="${H - 8}" text-anchor="middle">${esc(it.label)}</text>`;
        if (items.length <= 8 && it.value > 0) svg += `<text class="val" x="${x + bw / 2}" y="${top - 4}" text-anchor="middle">${money0(it.value)}</text>`;
      });
      svg += `<line class="axis" x1="${padL}" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>`;
      plot.innerHTML = `<svg viewBox="0 0 ${W} ${H}">${svg}</svg>`;
    } else {
      const lw = opts.labelWidth || 110, row = 30, padT = 6; const H = padT + row * items.length + 6; const x0 = lw + 8, x1 = W - 80;
      const x = (v) => x0 + (x1 - x0) * v / max;
      items.forEach((it, i) => {
        const yy = padT + row * i + 5; const bw = x(it.value) - x0;
        svg += `<rect class="hit" data-tip="${esc(it.tip)}" x="0" y="${padT + row * i}" width="${W}" height="${row}"/>`;
        svg += `<text class="cat" x="${lw}" y="${yy + 14}" text-anchor="end">${esc(truncate(it.label, lw / 6.2))}</text>`;
        svg += `<path class="bar" data-tip="${esc(it.tip)}" d="${roundRight(x0, yy, bw, 20)}"/>`;
        svg += `<text class="val" x="${x0 + bw + 6}" y="${yy + 14}">${(opts.fmt || money0)(it.value)}</text>`;
      });
      svg += `<line class="axis" x1="${x0}" x2="${x0}" y1="${padT}" y2="${H - 6}"/>`;
      plot.innerHTML = `<svg viewBox="0 0 ${W} ${H}">${svg}</svg>`;
    }
    attachTips(fig);
  };

  /** Sold (accent) against brought (context), one row per category, with a legend and percent label. */
  const stackChart = (fig, items) => {
    const plot = fig.querySelector('.plot');
    if (!items.length) { plot.innerHTML = '<div class="empty hint">No stock quantities on the product list.</div>'; return; }
    const W = fig.classList.contains('fig-wide') ? 1150 : 560, lw = 110, row = 30, padT = 6; const H = padT + row * items.length + 6; const x0 = lw + 8, x1 = W - 60;
    const max = Math.max(...items.map((i) => i.total)); const x = (v) => x0 + (x1 - x0) * v / max;
    let svg = '';
    items.forEach((it, i) => {
      const yy = padT + row * i + 5;
      svg += `<rect class="hit" data-tip="${esc(it.tip)}" x="0" y="${padT + row * i}" width="${W}" height="${row}"/>`;
      svg += `<text class="cat" x="${lw}" y="${yy + 14}" text-anchor="end">${esc(truncate(it.label, 17))}</text>`;
      svg += `<path class="bar ctx" data-tip="${esc(it.tip)}" d="${roundRight(x0, yy, x(it.total) - x0, 20)}"/>`;
      if (it.sold > 0) svg += `<rect class="bar" data-tip="${esc(it.tip)}" x="${x0}" y="${yy}" width="${Math.max(0, x(Math.min(it.sold, it.total)) - x0 - 2)}" height="20"/>`;
      svg += `<text class="val" x="${x(it.total) + 6}" y="${yy + 14}">${pct(it.sold / it.total * 100)}</text>`;
    });
    plot.innerHTML = `<div class="legend"><span><i style="background:var(--series-1)"></i>Sold</span><span><i style="background:var(--context)"></i>Still on the shelf</span></div><svg viewBox="0 0 ${W} ${H}">${svg}</svg>`;
    attachTips(fig);
  };

  const roundTop = (x, y, w, h, r = 4) => h <= 0 ? '' : `M${x},${y + h} V${y + Math.min(r, h)} Q${x},${y} ${x + Math.min(r, w / 2)},${y} H${x + w - Math.min(r, w / 2)} Q${x + w},${y} ${x + w},${y + Math.min(r, h)} V${y + h} Z`;
  const roundRight = (x, y, w, h, r = 4) => w <= 0 ? '' : `M${x},${y} H${x + w - Math.min(r, w)} Q${x + w},${y} ${x + w},${y + r} V${y + h - r} Q${x + w},${y + h} ${x + w - Math.min(r, w)},${y + h} H${x} Z`;
  const truncate = (t, n) => (t.length > n ? t.slice(0, n - 1) + '…' : t);

  // ------------------------------------------------------------ on-screen keys (touch devices)
  //
  // iPadOS treats a Bluetooth barcode scanner as a hardware keyboard and, once it
  // has typed, stops showing the on-screen keyboard. So on touch devices the
  // register carries its own keys: a PIN pad on the lock screen, a floating
  // numeric keypad for money and PIN fields, and a keyboard for the search box.
  // The fields keep inputmode="none" on touch so iOS never fights over them.

  const touch = window.matchMedia('(pointer: coarse)').matches;

  const typeInto = (input, key) => {
    if (key === 'back') input.value = input.value.slice(0, -1);
    else if (key === 'clear') input.value = '';
    else if (key === '.' && (input.value.includes('.') || input.dataset.numpad === 'pin')) return;
    else input.value += key;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  };

  const padKeys = (mode) => ['1', '2', '3', '4', '5', '6', '7', '8', '9', mode === 'pin' ? '' : '.', '0', 'back'];
  const keyHtml = (k) => (k === '' ? '<span></span>' : `<button type="button" data-key="${k}" aria-label="${k === 'back' ? 'Delete' : k}">${k === 'back' ? '⌫' : k}</button>`);

  // Lock screen PIN pad: inline, always visible on touch devices.
  $('lockPad').innerHTML = padKeys('pin').map(keyHtml).join('');
  $('lockPad').addEventListener('click', (e) => { const b = e.target.closest('[data-key]'); if (b) typeInto($('lockCode'), b.dataset.key); });

  // Floating keypad for any input marked data-numpad, opened on focus.
  const numpad = $('numpad');
  let padTarget = null;
  const closePad = () => { numpad.hidden = true; padTarget = null; };
  const openPad = (input) => {
    padTarget = input;
    numpad.innerHTML = padKeys(input.dataset.numpad).map(keyHtml).join('') + '<button type="button" class="done" data-key="done">Done</button>';
    numpad.hidden = false;
    const r = input.getBoundingClientRect(); const w = 232, h = numpad.offsetHeight || 260;
    let left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
    let top = r.bottom + 8;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 8);
    numpad.style.left = `${left}px`; numpad.style.top = `${top}px`;
  };
  numpad.addEventListener('pointerdown', (e) => e.preventDefault());   // keep the field focused
  numpad.addEventListener('click', (e) => {
    const b = e.target.closest('[data-key]'); if (!b || !padTarget) return;
    if (b.dataset.key === 'done') { padTarget.blur(); closePad(); return; }
    typeInto(padTarget, b.dataset.key);
  });
  if (touch) {
    document.querySelectorAll('[data-numpad]').forEach((input) => {
      input.setAttribute('inputmode', 'none');
      if (input.id !== 'lockCode') {
        input.addEventListener('focus', () => openPad(input));
        input.addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== input) closePad(); }, 120));
      }
    });
  }

  // Search keyboard: toggled by the key button beside the search box.
  const softkb = $('softkb');
  const KB_ROWS = [['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'], ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'], ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l', '-'], ['z', 'x', 'c', 'v', 'b', 'n', 'm', '_', '#', 'back'], ['clear', 'space', 'enter']];
  softkb.innerHTML = KB_ROWS.map((row) => `<div class="row">${row.map((k) => {
    const label = { back: '⌫', clear: 'Clear', space: 'Space', enter: 'Add' }[k] || k;
    const cls = k === 'space' ? ' class="space"' : ['clear', 'enter', 'back'].includes(k) ? ' class="wide"' : '';
    return `<button type="button"${cls} data-key="${k}">${label}</button>`;
  }).join('')}</div>`).join('');
  softkb.addEventListener('pointerdown', (e) => e.preventDefault());
  softkb.addEventListener('click', (e) => {
    const b = e.target.closest('[data-key]'); if (!b) return;
    const k = b.dataset.key; const input = $('searchInput');
    if (k === 'enter') { const code = input.value.trim(); if (code) scan(code); return; }
    typeInto(input, k === 'space' ? ' ' : k);
  });
  $('kbBtn').addEventListener('click', () => {
    softkb.hidden = !softkb.hidden;
    $('kbBtn').classList.toggle('on', !softkb.hidden);
    if (!softkb.hidden) $('searchInput').focus({ preventScroll: true });
  });

  // ------------------------------------------------------------ shell

  document.querySelectorAll('.nav-btn').forEach((b) => b.addEventListener('click', () => go(b.dataset.view)));
  document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', closeSheets));
  $('scrim').addEventListener('click', closeSheets);

  const renderConnection = () => {
    $('connPill').classList.toggle('offline', !navigator.onLine);
    $('connText').textContent = navigator.onLine ? 'Online' : 'Offline';
  };
  window.addEventListener('online', renderConnection);
  window.addEventListener('offline', renderConnection);

  const boot = async () => {
    renderConnection();
    state.config = await api('/api/config');
    chooseEvent();
    $('lockStaff').value = localStorage.getItem('pos.staff') || '';
    await loadNames();
    if (state.config.unlocked) { showApp(); await loadCatalogue(); }
    else showLock();
  };
  boot().catch((err) => { $('lockErr').textContent = err.message; showLock(); });
})();
