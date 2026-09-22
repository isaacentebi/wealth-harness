  (() => {
    'use strict';
    const I18N = {
      en: {
        title: 'Quarterly review', you: '← You', q: 'Q{n} {y}', origin: 'Quarterly review',
        pdf: 'Download PDF', quarters: 'Quarters', loadError: 'The review could not be loaded.',
        empty: 'There is no completed quarter with statements yet. Upload one and the first review appears after the quarter closes.',
        upload: 'Upload a statement',
        s_net_worth: 'Net worth', s_performance: 'Performance', s_allocation: 'Allocation against your policy',
        s_cash_flow: 'Cash flow', s_goals: 'Goals', s_decisions: 'Decisions this quarter', s_dca: 'Recurring investing',
        s_taxes: 'Taxes, estimated', s_fees: 'Fees', s_next: 'Two decisions for next quarter',
        portfolio: 'Your portfolio', twr: 'time-weighted', benchmark: 'Benchmark', difference: 'Difference: {v}',
        points: '{v} pts', range: 'Range {a}–{b}', outside: 'outside its range', target: 'target {v}',
        income: 'Income', spending: 'Spending', net: 'Saved', rate: 'Savings rate',
        st_on_track: 'On track', st_behind: 'Behind', st_funded: 'Funded', st_unknown: '—',
        funded: '{p} of {t} · {d}',
        noDecisions: 'No decisions were recorded this quarter.', openBefore: '{n} still open from before.',
        d_accepted: 'Accepted', d_proposed: 'Proposed', d_dismissed: 'Dismissed',
        trades: n => `${n} ${n === 1 ? 'trade' : 'trades'} since`, invested: '{v} invested',
        onTime: '{a} of {b} on time', ofPlanned: '{a} of {b}', missed: 'missed {d}', noPlans: 'No recurring plan is saved.',
        taxPeriod: 'This quarter', taxYtd: 'Year to date', j_MX: 'Mexico', j_US: 'United States',
        paid: 'Paid this quarter', annual: 'Annual cost', bps: '{a}–{b} bp a year', bp1: '{a} bp a year',
        talk: 'Talk about this', noNext: 'Nothing stands out for next quarter.', feesUnknown: 'The cost of your holdings is not known yet.',
        noPolicy: 'There is no accepted investment policy yet, so there are no ranges to check against.',
        printed: 'Prepared {d} by Wealth from your statements and saved picture.',
        ac_cash: 'Cash', ac_equity: 'Equity', ac_fixed_income: 'Fixed income', ac_fund: 'Funds', ac_unknown: 'Unclassified'
      },
      es: {
        title: 'Revisión trimestral', you: '← Tú', q: 'T{n} {y}', origin: 'Revisión trimestral',
        pdf: 'Descargar PDF', quarters: 'Trimestres', loadError: 'No se pudo cargar la revisión.',
        empty: 'Aún no hay un trimestre cerrado con estados de cuenta. Sube uno y la primera revisión aparece al cerrar el trimestre.',
        upload: 'Subir un estado de cuenta',
        s_net_worth: 'Patrimonio', s_performance: 'Rendimiento', s_allocation: 'Asignación contra tu política',
        s_cash_flow: 'Flujo de efectivo', s_goals: 'Metas', s_decisions: 'Decisiones del trimestre', s_dca: 'Inversión periódica',
        s_taxes: 'Impuestos estimados', s_fees: 'Comisiones', s_next: 'Dos decisiones para el próximo trimestre',
        portfolio: 'Tu portafolio', twr: 'ponderado por tiempo', benchmark: 'Referencia', difference: 'Diferencia: {v}',
        points: '{v} pts', range: 'Rango {a}–{b}', outside: 'fuera de rango', target: 'meta {v}',
        income: 'Ingresos', spending: 'Gastos', net: 'Ahorro', rate: 'Tasa de ahorro',
        st_on_track: 'En camino', st_behind: 'Atrasada', st_funded: 'Cumplida', st_unknown: '—',
        funded: '{p} de {t} · {d}',
        noDecisions: 'No se registraron decisiones este trimestre.', openBefore: '{n} siguen abiertas desde antes.',
        d_accepted: 'Aceptada', d_proposed: 'Propuesta', d_dismissed: 'Descartada',
        trades: n => `${n} ${n === 1 ? 'operación' : 'operaciones'} desde el`, invested: '{v} invertidos',
        onTime: '{a} de {b} a tiempo', ofPlanned: '{a} de {b}', missed: 'faltó {d}', noPlans: 'No hay un plan periódico guardado.',
        taxPeriod: 'Este trimestre', taxYtd: 'En lo que va del año', j_MX: 'México', j_US: 'Estados Unidos',
        paid: 'Pagadas este trimestre', annual: 'Costo anual', bps: '{a}–{b} pb al año', bp1: '{a} pb al año',
        talk: 'Hablar de esto', noNext: 'Nada destaca para el próximo trimestre.', feesUnknown: 'Aún no se conoce el costo de tus inversiones.',
        noPolicy: 'Aún no hay una política de inversión aceptada; por eso no hay rangos con los que comparar.',
        printed: 'Preparado el {d} por Wealth con tus estados de cuenta y tu panorama guardado.',
        ac_cash: 'Efectivo', ac_equity: 'Acciones', ac_fixed_income: 'Renta fija', ac_fund: 'Fondos', ac_unknown: 'Sin clasificar'
      }
    };
    const root = document.getElementById('root');
    let lang = 'en', locale = 'en-US', token = '', data = null;
    let period = (() => { try { const p = new URLSearchParams(location.search).get('period'); return /^\d{4}-Q[1-4]$/.test(p || '') ? p : null; } catch (_) { return null; } })();

    // ------------------------------------------------------------ helpers (DOM only; never innerHTML)
    function t(key, vars) {
      const text = (I18N[lang] && I18N[lang][key]) ?? I18N.en[key] ?? key;
      return vars && typeof text === 'string' ? text.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? '')) : text;
    }
    function h(tag, props, ...kids) {
      const el = document.createElement(tag);
      for (const [k, v] of Object.entries(props || {})) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
        else if (k === 'style') Object.assign(el.style, v);
        else el.setAttribute(k, v === true ? '' : String(v));
      }
      for (const kid of kids.flat(Infinity)) {
        if (kid === null || kid === undefined || kid === false || kid === '') continue;
        el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
      }
      return el;
    }
    const L = value => typeof value === 'string' ? value : value && typeof value === 'object' ? String(value[lang] || value.en || '') : '';
    const n = raw => { if (raw === null || raw === undefined || raw === '' || typeof raw === 'boolean') return null; const x = Number(raw); return Number.isFinite(x) ? x : null; };
    const minus = text => text.replace(/-(?=\D?\d|\p{Sc})/u, '−');
    const ISO = /^\d{4}-\d{2}(-\d{2})?$/;
    function date(iso, style = 'medium') {
      if (typeof iso !== 'string' || !ISO.test(iso)) return '—';
      const [y, m, d] = iso.split('-').map(Number);
      const opts = style === 'month' || !d ? { month: 'short', year: 'numeric' } : { day: 'numeric', month: 'short', year: 'numeric' };
      return new Intl.DateTimeFormat(locale, { ...opts, timeZone: 'UTC' }).format(new Date(Date.UTC(y, m - 1, d || 15, 12))).replace('.', '');
    }
    // The same value primitives the chat's views draw: {t, v, cur}; v null is unknown and reads "—".
    function V(value, opts = {}) {
      if (!value || typeof value !== 'object') return '—';
      if (value.t === 'date') return date(value.v, opts.style);
      if (value.t === 'range') {
        const a = V({ t: 'money', v: value.lo, cur: value.cur }), b = V({ t: 'money', v: value.hi, cur: value.cur });
        return a === '—' || b === '—' ? '—' : a === b ? a : `${a}–${b}`;
      }
      const x = n(value.v);
      if (x === null) return '—';
      if (value.t === 'money') {
        const places = Math.abs(x) >= 100 || Number.isInteger(x) ? 0 : 2;
        return minus(new Intl.NumberFormat(locale, { style: 'currency', currency: value.cur || 'MXN', currencyDisplay: 'narrowSymbol', minimumFractionDigits: places, maximumFractionDigits: places, signDisplay: opts.signed ? 'exceptZero' : 'auto' }).format(x));
      }
      if (value.t === 'ratio') return minus(new Intl.NumberFormat(locale, { style: 'percent', minimumFractionDigits: opts.places === 'auto' ? 0 : opts.places ?? 1, maximumFractionDigits: opts.places === 'auto' ? 2 : opts.places ?? 1, signDisplay: opts.signed ? 'exceptZero' : 'auto' }).format(x));
      return minus(new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(x));
    }
    const quarterName = label => { const m = /^(\d{4})-Q([1-4])$/.exec(label || ''); return m ? t('q', { n: m[2], y: m[1] }) : ''; };
    // The quarter in progress reads as partial ("T3 2026 · en curso"); closed quarters by their name.
    const periodName = label => data && data.current && label === data.current.period ? L(data.current.label) : quarterName(label);
    const note = value => value ? h('p', { class: 'note', text: '— ' + L(value) }) : null;
    function part(no, key, ...kids) {
      return h('section', { class: 'part', 'aria-labelledby': 'h-' + key },
        h('span', { class: 'num', 'aria-hidden': 'true', text: String(no).padStart(2, '0') }),
        h('h2', { id: 'h-' + key, text: t('s_' + key) }), kids);
    }
    function row(label, value, { sub, total, sign } = {}) {
      return h('div', { class: 'row' + (total ? ' total' : '') },
        h('dt', {}, label, sub ? h('span', { class: 'sub' }, sub) : null),
        h('dd', {}, sign && value !== '—' ? h('span', { class: 'sign', 'aria-hidden': 'true', text: sign }) : null, h('span', { class: 'v', text: value })));
    }
    // "Talk about this": the chat opens with the prompt in the composer, not sent (the person sends it).
    function handoff(text, send) {
      try { sessionStorage.setItem('wealth.handoff', JSON.stringify({ text, send: !!send, lang })); } catch (_) { /* the chat still opens */ }
      location.href = '/';
    }

    // ------------------------------------------------------------ sections
    function netWorth(s) {
      // Each movement carries one sign: a negative value reads "− $603", never "+ −$603".
      const rows = s.rows.map((r, i) => {
        const v = V(r.value), neg = v.startsWith('−');
        return row(L(r.label), neg ? v.slice(1) : v, { sub: r.date ? V(r.date) : null, sign: i ? (neg ? '−' : '+') : null });
      });
      return [h('dl', { class: 'ticket' }, rows, row(L(s.total.label), V(s.total.value), { sub: s.total.date ? V(s.total.date) : null, total: true, sign: '=' })), note(s.note)];
    }
    // The benchmark is drawn from its parts (weight + index name, both in the page language), never from a free-text name.
    function composition(parts) {
      // Each part keeps to one line where the column allows ("35% CETES 364 días"); the parts wrap between each
      // other, and on a phone a long part wraps inside itself rather than running off the page.
      const names = (parts || []).map(p => `${V(p.weight, { places: 'auto' })} ${L(p.index)}`).filter(x => !x.startsWith('—'));
      if (!names.length) return null;
      const out = [];
      names.forEach((name, i) => { if (i) out.push(' + '); out.push(h('span', { class: 'part', text: name })); });
      return out;
    }
    function performance(s) {
      const diff = n(s.difference?.v);
      const b = s.benchmark || {};
      return [h('dl', { class: 'ticket' },
          row(t('portfolio'), V(s.portfolio), { sub: t('twr') }),
          row(L(b.label) || t('benchmark'), V(b.value), { sub: composition(b.parts) })),
        diff !== null ? h('p', { class: 'note mono', text: t('difference', { v: t('points', { v: minus(new Intl.NumberFormat(locale, { maximumFractionDigits: 1, signDisplay: 'exceptZero' }).format(diff * 100)) }) }) }) : null,
        note(s.note)];
    }
    function allocation(s) {
      const sleeves = s.sleeves || [];
      // Without an accepted policy there are no ranges to hold the weights against: one line says so, no bars.
      const bands = sleeves.some(x => n(x.min?.v) !== null || n(x.max?.v) !== null);
      const weighed = sleeves.some(x => n(x.weight?.v) !== null);
      if (!bands || !weighed) return note(s.note) || h('p', { class: 'quiet', style: { margin: 0 }, text: t('noPolicy') });
      const top = Math.min(1, Math.max(0.1, ...sleeves.map(x => Math.max(n(x.weight?.v) ?? 0, n(x.max?.v) ?? 0))) * 1.15);
      const at = v => `${Math.min(100, (v / top) * 100).toFixed(2)}%`;
      return [h('div', {}, sleeves.map(x => {
        const w = n(x.weight?.v), lo = n(x.min?.v), hi = n(x.max?.v);
        const name = L(x.name) || I18N[lang]['ac_' + x.id] || x.id;
        const sub = lo !== null && hi !== null ? [t('range', { a: V(x.min, { places: 'auto' }), b: V(x.max, { places: 'auto' }) }), x.outside ? h('span', { class: 'out', text: ' · ' + t('outside') }) : null] : null;
        return h('div', { class: 'sleeve' },
          h('div', { class: 'line' }, h('span', { class: 'name', text: name }), h('span', { class: 'v', text: V(x.weight) })),
          h('div', { class: 'track', 'aria-hidden': 'true' },
            w !== null ? h('span', { class: 'bar', style: { width: at(w) } }) : null,
            lo !== null ? h('span', { class: 'tick', style: { left: at(lo) } }) : null,
            hi !== null ? h('span', { class: 'tick', style: { left: at(hi) } }) : null),
          sub ? h('div', { class: 'sub' }, sub) : null);
      })), note(s.note)];
    }
    function cashFlow(s, prev) {
      const cur = s.current || {}, pri = s.prior || {};
      const tr = (key, field, total) => h('tr', { class: total ? 'total' : null },
        h('th', { scope: 'row', text: t(key) }), h('td', { text: V(cur[field], { places: 0 }) }), h('td', { class: 'prior', text: V(pri[field], { places: 0 }) }));
      return [h('table', { class: 'flow' },
          h('thead', {}, h('tr', {}, h('th', {}, h('span', { class: 'sr', text: t('s_cash_flow') })), h('th', { scope: 'col', text: periodName(data.period) }), h('th', { scope: 'col', text: quarterName(prev) }))),
          h('tbody', {}, tr('income', 'income'), tr('spending', 'spending'), tr('net', 'net'), tr('rate', 'savings_rate', true))),
        note(s.note)];
    }
    function goals(s) {
      if (!(s.items || []).length) return note(s.note);
      return [h('div', {}, s.items.map(g => {
        const p = n(g.funded?.v);
        // Only what is known: "— de — · —" says nothing, so a goal with no figures shows its name alone.
        const known = [V(g.funded), V(g.target), V(g.by, { style: 'month' })].some(x => x !== '—');
        return h('div', { class: 'item' },
          h('div', { class: 'line' }, h('span', { class: 'what', text: g.name }), h('span', { class: 'v', text: t('st_' + g.status) })),
          p !== null ? h('div', { class: 'fill', 'aria-hidden': 'true' }, h('span', { style: { width: `${Math.max(0, Math.min(1, p)) * 100}%` } })) : null,
          known ? h('div', { class: 'sub', text: t('funded', { p: V(g.funded), t: V(g.target), d: V(g.by, { style: 'month' }) }) }) : null);
      })), note(s.note)];
    }
    function decisions(s) {
      const items = s.items || [];
      return [items.length ? h('div', {}, items.map(d => {
        const trades = n(d.trades?.v);
        const bits = [t('d_' + d.status) || d.status, V(d.on),
          trades !== null ? `${t('trades')(trades)} ${V(d.on)}` : null,
          n(d.invested?.v) !== null ? t('invested', { v: V(d.invested) }) : null].filter(Boolean);
        return h('div', { class: 'item' }, h('div', { class: 'what', text: d.what }), h('div', { class: 'sub', text: bits.join(' · ') }));
      })) : h('p', { class: 'quiet', style: { margin: 0 }, text: t('noDecisions') }),
        s.open_before ? h('p', { class: 'note', text: t('openBefore', { n: s.open_before }) }) : null, note(s.note)];
    }
    function dca(s) {
      const items = s.items || [];
      if (!items.length) return [h('p', { class: 'quiet', style: { margin: 0 }, text: t('noPlans') }), note(s.note)];
      return [h('div', {}, items.map(p => h('div', { class: 'item' },
        h('div', { class: 'line' }, h('span', { class: 'what', text: L(p.name) }), h('span', { class: 'v', text: t('onTime', { a: V(p.on_time), b: V(p.installments) }) })),
        h('div', { class: 'sub', text: [t('ofPlanned', { a: V(p.invested), b: V(p.planned) }), ...(p.missed || []).map(d => t('missed', { d: V(d) }))].join(' · ') })))),
        note(s.note)];
    }
    function taxes(s) {
      const where = s.jurisdiction ? (I18N[lang]['j_' + s.jurisdiction] || s.jurisdiction) : null;
      return [h('dl', { class: 'ticket' }, row(t('taxPeriod'), V(s.period), { sub: where }), row(t('taxYtd'), V(s.ytd))), note(s.note)];
    }
    function fees(s) {
      const lo = n(s.bps?.lo), hi = n(s.bps?.hi);
      const fmt = x => new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(x);
      const bps = lo === null ? null : hi === null || Math.round(lo) === Math.round(hi) ? t('bp1', { a: fmt(lo) }) : t('bps', { a: fmt(lo), b: fmt(hi) });
      // The floor note describes a known floor; when the annual cost itself is unknown it says so instead.
      const annualUnknown = V(s.annual) === '—';
      // A zero paid while some costs are unknown is not a real zero: it reads "—".
      const paid = n(s.paid?.v) === 0 && !s.complete ? '—' : V(s.paid);
      return [h('dl', { class: 'ticket' }, row(t('paid'), paid), row(t('annual'), V(s.annual), { sub: bps })),
        annualUnknown ? h('p', { class: 'note', text: '— ' + t('feesUnknown') }) : note(s.note)];
    }
    // Sections with nothing to say leave the letter: no decisions, no tax and no fee are not findings.
    const zeroOrUnknown = value => { const x = n(value?.v); return x === null || x === 0; };
    const hasDecisions = s => (s.items || []).length > 0 || Number(s.open_before) > 0;
    const hasTaxes = s => !(zeroOrUnknown(s.period) && zeroOrUnknown(s.ytd));
    const hasFees = s => !(zeroOrUnknown(s.paid) && V(s.annual) === '—');
    function next(s) {
      const items = s.items || [];
      if (!items.length) return h('p', { class: 'quiet', style: { margin: 0 }, text: t('noNext') });
      return h('ol', { class: 'next' }, items.map((x, i) => h('li', {},
        h('span', { class: 'n', text: String(i + 1) }),
        h('div', {},
          h('p', { class: 'title', text: L(x.title) }),
          n(x.value?.v) !== null ? h('div', { class: 'value', text: V(x.value) }) : null,
          h('button', { class: 'txt talk', type: 'button', text: t('talk'), onclick: () => handoff(L(x.prompt), false) })))));
    }

    // ------------------------------------------------------------ page
    function render() {
      document.title = `${t('title')} · Wealth`;
      document.getElementById('back').textContent = t('you');
      if (!data) return;
      const quarters = [...(data.current ? [data.current.period] : []), ...(data.quarters || [])];
      const header = [
        data.sections ? h('p', { class: 'origin', text: t('origin') }) : null,
        // With no quarter to review yet, the page is not headlined with one it cannot show.
        h('h1', {}, data.sections ? periodName(data.period) : t('title')),
        h('div', { class: 'controls' },
          quarters.length > 1 ? h('nav', { class: 'quarters', 'aria-label': t('quarters') }, quarters.slice(0, 6).map(q =>
            h('a', { href: '/review?period=' + q, 'aria-current': q === data.period ? 'page' : null, text: periodName(q),
              onclick: e => { e.preventDefault(); if (q !== data.period) { period = q; history.replaceState(null, '', '/review?period=' + q); load(); } } }))) : h('span'),
          data.sections ? h('button', { class: 'txt', type: 'button', text: t('pdf'), onclick: () => window.print() }) : null)].filter(Boolean);
      if (!data.sections) {
        root.replaceChildren(...header, h('p', { class: 'empty', text: t('empty') }),
          h('a', { class: 'txt act', href: '/', text: t('upload') }));
        return;
      }
      const s = data.sections;
      const prev = (() => { const m = /^(\d{4})-Q([1-4])$/.exec(data.period); if (!m) return ''; const q = +m[2], y = +m[1]; return q === 1 ? `${y - 1}-Q4` : `${y}-Q${q - 1}`; })();
      // Sections with nothing known are left out (their numbers follow what is shown): no heading over
      // "nothing stands out", no "$0" where the cost is unknown.
      const unknown = value => V(value) === '—';
      const shown = [
        ['net_worth', netWorth(s.net_worth)], ['performance', performance(s.performance)],
        ['allocation', allocation(s.allocation)], ['cash_flow', cashFlow(s.cash_flow, prev)], ['goals', goals(s.goals)],
        ['decisions', hasDecisions(s.decisions) ? decisions(s.decisions) : null], ['dca', dca(s.dca)],
        ['taxes', hasTaxes(s.taxes) ? taxes(s.taxes) : null],
        ['fees', unknown(s.fees.paid) && unknown(s.fees.annual) ? null : hasFees(s.fees) ? fees(s.fees) : null],
        ['next', (s.next_quarter.items || []).length ? next(s.next_quarter) : null]].filter(([, body]) => body !== null);
      const letter = data.letter || {};
      const narrative = letter.narrative && letter.narrative[lang];
      const sentences = narrative ? [narrative] : ((letter.summary || {})[lang] || []);
      root.replaceChildren(...header,
        h('div', { class: 'letter', 'data-slot': 'narrative' },
          data.name ? h('p', { class: 'salute', text: lang === 'es' ? `${data.name}:` : `${data.name},` }) : null,
          sentences.map(text => h('p', { class: 'body', text }))),
        ...shown.map(([key, body], i) => part(i + 1, key, body)),
        h('p', { class: 'printed', text: t('printed', { d: date(data.today) }) }));
    }

    async function load() {
      root.setAttribute('aria-busy', 'true');
      try {
        if (!token) {
          const state = await fetch('/api/state', { credentials: 'same-origin', cache: 'no-store' }).then(r => r.ok ? r.json() : {}).catch(() => ({}));
          token = String(state.csrf_token || '');
        }
        const response = await fetch('/api/review' + (period ? '?period=' + encodeURIComponent(period) : ''),
          { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Wealth-Token': token } });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || t('loadError'));
        data = body;
        period = data.period;
        render();
      } catch (error) {
        root.replaceChildren(h('p', { class: 'empty', text: error instanceof Error && error.message ? error.message : t('loadError') }));
      } finally { root.setAttribute('aria-busy', 'false'); }
    }

    function setLang(next, persist) {
      lang = next === 'es' ? 'es' : 'en';
      locale = lang === 'es' ? 'es-MX' : 'en-US';
      document.documentElement.lang = lang === 'es' ? 'es-MX' : 'en';
      if (persist) { try { localStorage.setItem('wealth.lang', lang); } catch (_) { /* optional */ } }
      document.querySelectorAll('#lang button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.lang === lang)));
      render();
    }
    document.querySelectorAll('#lang button').forEach(b => b.addEventListener('click', () => setLang(b.dataset.lang, true)));
    let stored = null;
    try { stored = localStorage.getItem('wealth.lang'); } catch (_) { /* optional */ }
    let asked = null;
    try { asked = new URLSearchParams(location.search).get('lang'); } catch (_) { /* optional */ }
    // The saved language (/api/state) decides the first paint too, so the letter never flips language after loading.
    (async () => {
      const state = await fetch('/api/state', { credentials: 'same-origin', cache: 'no-store' }).then(r => r.ok ? r.json() : {}).catch(() => ({}));
      token = String(state.csrf_token || '');
      const saved = ['es', 'en'].includes(state.language) ? state.language : null;
      setLang(asked || stored || saved || ((navigator.language || '').toLowerCase().startsWith('es') ? 'es' : 'en'), false);
      load();
    })();
  })();
