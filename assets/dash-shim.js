// A small stand-in for the dashboard runtime the page was first built on.
// It serves the bundled datasets (window.CYC_DATA), runs the derived calculations,
// and redraws every [data-draw] element after load and on resize.
(function () {
  const raw = window.CYC_DATA || {};
  const calcs = {};
  const memo = {};
  const draws = {};
  const onDatas = [];

  function compute(id, stack) {
    if (Object.prototype.hasOwnProperty.call(raw, id)) return raw[id];
    if (Object.prototype.hasOwnProperty.call(memo, id)) return memo[id];
    const c = calcs[id];
    if (!c) return undefined;
    stack = stack || [];
    if (stack.includes(id)) return undefined;
    const args = c.inputs.map((i) => compute(i, stack.concat(id)));
    if (args.some((a) => a === undefined)) return undefined;
    try {
      memo[id] = c.fn(...args);
    } catch (e) {
      console.error('calculation failed:', id, e);
      memo[id] = undefined;
    }
    return memo[id];
  }

  function runDraw(name) {
    const fn = draws[name];
    if (!fn) return;
    document.querySelectorAll('[data-draw="' + name + '"]').forEach((el) => {
      try {
        if (typeof fn === 'function') fn(el);
        else {
          if (!el.__made && fn.mount) el.__made = fn.mount(el);
          if (fn.update) fn.update(el, el.__made);
        }
      } catch (e) {
        console.error('drawing failed:', name, e);
      }
    });
  }

  function runAll() {
    onDatas.forEach((f) => { try { f(); } catch (e) { console.error(e); } });
    Object.keys(draws).forEach(runDraw);
  }

  window.dash = {
    colors: ['var(--series-1)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)'],
    calc(id, o) { calcs[id] = o; },
    loader() {},
    data(id) {
      const v = compute(id);
      if (v === undefined) return { status: 'error', data: [], message: 'missing: ' + id };
      return { status: 'ok', data: v, columns: Array.isArray(v) && v[0] ? Object.keys(v[0]) : [] };
    },
    draw(name, fn) {
      if (fn) { draws[name] = fn; return; }
      runDraw(name);
    },
    onData(fn) { onDatas.push(fn); },
    params() { return {}; },
    setParams() {},
    resetParams() {},
    setLink() {},
    refresh() {}
  };

  window.dashBoot = function () {
    runAll();
    let t = null;
    let lastW = window.innerWidth;
    window.addEventListener('resize', () => {
      if (window.innerWidth === lastW) return;
      lastW = window.innerWidth;
      clearTimeout(t);
      t = setTimeout(runAll, 200);
    });
  };
})();
