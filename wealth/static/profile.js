  (() => {
    'use strict';
    const I18N = {
      en: {
        you: 'You', chat: '← Chat', loadError: 'Could not load your profile.',
        netWorth: 'Net worth', asOfK: 'as of {d}', without: 'Not counting {x}', unknownWorth: 'Not known yet: {x} has no balance.',
        liquid: 'Liquid', illiquid: 'Illiquid', debts: 'Debts', worthTable: 'Net worth split',
        history: '{x} · {p} of your assets', since: 'since {d}', nowK: 'now',
        today: 'Today',
        month: 'Your month', savingsRate: 'savings rate', monthsAvg: 'average of {n} months of transactions', stated: 'from what you told me',
        incomeIn: 'Coming in', f_essentials: 'Essentials', f_other: 'Other spending', f_spending: 'Spending', f_debt: 'Debt payments',
        f_committed: 'Committed to goals', f_unallocated: 'Unallocated', f_short: 'Short by', ofIncome: 'of income',
        debtUnknown: 'Not counting the payment on {x}: I don’t know it.', missIncome: 'I don’t know how much comes in each month.',
        missSpending: 'I don’t know how much you spend.', incomeOnly: '{x} comes in each month.', tellMe: 'Tell me',
        askIncome: 'Each month I take home ', askSpending: 'Each month I spend about ',
        byMonth: 'Spending by month', avg: 'avg {x}', topCats: 'Where most of it goes', monthsTable: 'Spending by month', catsTable: 'Top spending categories',
        have: 'What you own', inAssets: '{x} in assets', byClass: 'By type', byCurrency: 'By currency', topHold: 'Largest holdings',
        ofAssets: 'of your assets', overlapTimes: { 2: 'twice', 3: 'three times', 4: 'four times' },
        venue: 'Where it trades', domicile: 'Where it is domiciled', v_sic: 'SIC', v_bmv: 'BMV', v_abroad: 'Broker abroad', v_unknown: 'Not stated',
        d_us: 'US-situs', d_non_us: 'Outside the US', d_unknown: 'Not stated', splitNote: 'Securities only, cash excluded.',
        accounts: 'Accounts', asOfShort: 'as of {d}', saidOn: 'you said, {d}', balUnknown: 'balance unknown', fxUnknown: 'no exchange rate', pricesUnknown: 'no prices',
        ac_equity: 'Stocks', ac_fund: 'Funds and ETFs', ac_fixed_income: 'Fixed income', ac_cash: 'Cash', ac_real_estate: 'Real estate', ac_retirement: 'Retirement', ac_crypto: 'Crypto', ac_unclassified: 'Unclassified',
        owe: 'What you owe', total: 'Total',
        missRate: 'the interest rate', missPayment: 'the monthly payment', missing: 'Missing {x}.', askDebt: 'My {x}: rate of ', never: 'At this payment it is never paid off.',
        payoffTable: 'Balance over time', monthsFromNow: 'Months from now', balance: 'Balance',
        goals: 'Goals', s_on_track: 'On track', s_behind: 'Behind', s_funded: 'Funded', s_unknown: 'No date or target',
        by: 'by {d}', needs: 'Needs {n} a month; you put in {m}.', noPlanMonthly: 'No monthly amount yet.',
        projected: 'at this pace: {x}', monthlyGoal: '{x} a month', plan: 'Plan {x}', onTime: '{a} of {b} on time', invested: '{a} of {b} invested',
        st_on_time: 'On time', st_late: 'Late', st_partial: 'Partial', st_skipped: 'Missed', st_pending: 'Pending', st_unknown: 'Unknown',
        dotsTable: 'Contributions by month', goalTable: 'Progress', noGoals: 'I don’t know your goals yet.', askGoals: 'One goal I have is ',
        reserve: 'Emergency fund', ofMonths: 'of {t} months', target: 'Target', atPace: 'At this pace',
        rateK: 'a year', perMonthK: 'a month', interestK: 'interest to go', debtsAsk: 'Do you owe anything?', askDebts: 'I owe ', monthsK: 'months', gaugeTarget: 'target {t}', gapLeft: '{a} of {b} · {g} to go',
        reserveNoTarget: 'No target yet.', askReserve: 'I want my emergency fund to cover ',
        reserveMet: 'Covered.', reserveTable: 'Emergency fund',
        returns: 'Returns', p_3m: '3M', p_1y: '1Y', p_all: 'All', periods: 'Period', twr: 'Your portfolio',
        mwr: 'Your personal return', bench: 'Reference', you2: 'Your portfolio',
        returnsNo: 'Not enough history to measure returns yet.', returnsPrices: 'Missing daily prices for {x}, so returns stay unknown.',
        returnsLeft: 'Not counting {x}: daily prices are missing.', returnsTable: 'Portfolio vs reference', review: 'Quarterly review',
        flowsMissing: 'Deposits and withdrawals between statements are missing.', upload: 'Upload a statement',
        knows: 'What I know about you', factsN: '{n} facts', dueOne: '1 figure to confirm.', dueN: '{n} figures to confirm.', reviewNow: 'Review',
        passOf: '{i} of {n}', keepGo: 'Continue', later: 'Later', passDone: 'All up to date.', historySource: 'History and source', moreOptions: 'More options',
        g_money_in: 'Money coming in', g_money_out: 'What you spend', g_own: 'What you own', g_owe: 'What you owe',
        g_goals: 'Goals', g_invest: 'How you invest', g_about: 'About you',
        unknownHead: 'Not known yet', monthsShort: '{n} months of transactions', leftLine: '{x} left unallocated', shortLine: 'Short by {x}', avgLine: 'avg {x} a month',
        estate: 'Estate', estateScore: '{n}% complete', estateClear: 'No gaps in what you have told me.',
        monthDetail: 'Your month in detail', inAssetsShort: 'in assets', accountsSum: '{n} accounts', accountsSumUnknown: '{n} accounts · {u} without a figure',
        sincePeriod: 'since {d}', paidBy: 'paid off by {d}', fundedUnknown: 'No account is tied to this goal yet, so the progress is not known.', st_next: 'Next',
        connsSum: '{n} connected · {m} not connected', connsNone: 'Nothing connected yet · statements by upload', staleN: '{n} to refresh',
        save: 'Save', cancel: 'Cancel', forget: 'Forget', close: 'Close', amount: 'Amount', currency: 'Currency',
        months: 'months', saved: 'Saved.', confirmed: 'Thanks, noted.', undo: 'Undo', forgetting: 'I’ll forget that.',
        emptyLine: 'I don’t know anything about your money yet.', tell: 'Tell me in the chat',
        more: '{n} more things',
       
       
       
       
        forgottenOn: 'Forgotten', replacedOn: 'Replaced by a statement', now: 'now', source: 'Source', recorded: 'Recorded', status: 'Status',
       
        src_you_said: 'You told me', src_document: 'Statement', src_web: 'Web', src_derived: 'Calculated',
       
        writesOff: 'Editing here isn’t available yet. Tell Wealth in the chat.', conflict: 'Your profile changed. Reloaded; try again.',
       
        todayDay: 'today', tomorrow: 'tomorrow',
        conns: 'Connections and data', keyKeychain: 'Key in keychain', keyEnv: 'Key in environment', noKey: 'Not connected',
        cap_ibkr_flex: 'Read only: positions, trades, dividends and cash', cap_alpaca: 'Read only: positions, activity and dividends',
        cap_cuenca: 'Read only: balance, transactions and savings pockets', cap_statements: 'Read only: whatever the PDF or CSV holds',
        statements: 'Statements', uploadRow: 'Upload a statement', synced: 'Synced {t}',
        sync: 'Sync', howTo: 'How to connect', unknownS: 'Unknown',
        syncAsk: 'Sync my {i} account (read only) and show me the summary before saving anything.',
        syncAskIbkr: ' My Flex query id is ', uploadAsk: 'I’m attaching a statement.',
        sheetTitle: 'Connect {i}', step1_ibkr_flex: 'In the IBKR portal create an Activity Flex Query (XML, last 365 days), then enable Flex Web Service and copy the current token.',
        step1_alpaca: 'In the Alpaca dashboard generate an API key and copy the key id and the secret (the secret is shown once).',
        step1_cuenca: 'Ask Cuenca for an API key and secret for your account. Without one, upload the monthly statement instead.',
        step2: 'Store it yourself from Terminal; macOS asks for the value. Never paste it into the chat.',
        step3: 'Or, for one session only, export:', step4: 'Then tap Sync here.', step4_ibkr_flex: 'Then tap Sync here and have your query id ready.',
        readOnlyLine: 'Wealth only reads. It cannot trade or move money.', copy: 'Copy', copied: 'Copied.',
        taxPack: 'Tax pack {y} for your CPA (printable)', taxPackFailed: 'The tax pack could not be downloaded.',
        export: 'Download everything (JSON)', erase: 'Delete my data', eraseLine: 'Only from the Terminal, so nothing does it by accident.', exportFailed: 'The export could not be downloaded.',
        date: 'Date', item: 'Item', value: 'Value', share: 'Share',
      },
      es: {
        you: 'Tú', chat: '← Chat', loadError: 'No se pudo cargar tu perfil.',
        netWorth: 'Patrimonio neto', asOfK: 'al {d}', without: 'Sin contar {x}', unknownWorth: 'Aún no se sabe: {x} no tiene saldo.',
        liquid: 'Líquido', illiquid: 'Ilíquido', debts: 'Deudas', worthTable: 'Cómo se compone tu patrimonio',
        history: '{x} · {p} de tus activos', since: 'desde el {d}', nowK: 'hoy',
        today: 'Hoy',
        month: 'Tu mes', savingsRate: 'tasa de ahorro', monthsAvg: 'promedio de {n} meses de movimientos', stated: 'de lo que me dijiste',
        incomeIn: 'Entra', f_essentials: 'Gastos esenciales', f_other: 'Otros gastos', f_spending: 'Gastos', f_debt: 'Pagos de deudas',
        f_committed: 'Comprometido a metas', f_unallocated: 'Sin asignar', f_short: 'Te faltan', ofIncome: 'del ingreso',
        debtUnknown: 'Sin contar el pago de {x}: no lo conozco.', missIncome: 'No sé cuánto te entra al mes.',
        missSpending: 'No sé cuánto gastas.', incomeOnly: 'Te entran {x} al mes.', tellMe: 'Dímelo',
        askIncome: 'Al mes me entran ', askSpending: 'Al mes gasto como ',
        byMonth: 'Gasto por mes', avg: 'prom. {x}', topCats: 'En qué se va', monthsTable: 'Gasto por mes', catsTable: 'Categorías de gasto principales',
        have: 'Lo que tienes', inAssets: '{x} en activos', byClass: 'Por tipo', byCurrency: 'Por moneda', topHold: 'Posiciones principales',
        ofAssets: 'de tus activos', overlapTimes: { 2: 'dos veces', 3: 'tres veces', 4: 'cuatro veces' },
        venue: 'Dónde cotiza', domicile: 'Domicilio', v_sic: 'SIC', v_bmv: 'BMV', v_abroad: 'Broker en el extranjero', v_unknown: 'Sin dato',
        d_us: 'EE. UU.', d_non_us: 'Fuera de EE. UU.', d_unknown: 'Sin dato', splitNote: 'Solo valores; sin efectivo.',
        accounts: 'Cuentas', asOfShort: 'al {d}', saidOn: 'me dijiste, {d}', balUnknown: 'saldo desconocido', fxUnknown: 'sin tipo de cambio', pricesUnknown: 'sin precios',
        ac_equity: 'Acciones', ac_fund: 'Fondos y ETF', ac_fixed_income: 'Renta fija', ac_cash: 'Efectivo', ac_real_estate: 'Inmuebles', ac_retirement: 'Retiro', ac_crypto: 'Cripto', ac_unclassified: 'Sin clasificar',
        owe: 'Lo que debes', total: 'Total',
        missRate: 'la tasa de interés', missPayment: 'el pago mensual', missing: 'Falta {x}.', askDebt: 'Mi {x}: tasa de ', never: 'Con este pago nunca se termina de pagar.',
        payoffTable: 'Saldo con el tiempo', monthsFromNow: 'Meses a partir de hoy', balance: 'Saldo',
        goals: 'Metas', s_on_track: 'En camino', s_behind: 'Atrasada', s_funded: 'Cumplida', s_unknown: 'Sin fecha ni monto',
        by: 'para {d}', needs: 'Necesita {n} al mes; aportas {m}.', noPlanMonthly: 'Aún sin aportación mensual.',
        projected: 'a este ritmo: {x}', monthlyGoal: '{x} al mes', plan: 'Plan {x}', onTime: '{a} de {b} a tiempo', invested: '{a} de {b} invertidos',
        st_on_time: 'A tiempo', st_late: 'Tarde', st_partial: 'Parcial', st_skipped: 'Omitida', st_pending: 'Pendiente', st_unknown: 'Desconocida',
        dotsTable: 'Aportaciones por mes', goalTable: 'Avance', noGoals: 'Aún no sé tus metas.', askGoals: 'Una meta que tengo es ',
        reserve: 'Fondo de emergencia', ofMonths: 'de {t} meses', target: 'Meta', atPace: 'A este ritmo',
        rateK: 'anual', perMonthK: 'al mes', interestK: 'de intereses por pagar', debtsAsk: '¿Debes algo?', askDebts: 'Debo ', monthsK: 'meses', gaugeTarget: 'meta {t}', gapLeft: '{a} de {b} · faltan {g}',
        reserveNoTarget: 'Aún sin meta.', askReserve: 'Quiero que mi fondo de emergencia cubra ',
        reserveMet: 'Cubierto.', reserveTable: 'Fondo de emergencia',
        returns: 'Rendimiento', p_3m: '3M', p_1y: '1A', p_all: 'Todo', periods: 'Periodo', twr: 'Tu portafolio',
        mwr: 'Tu rendimiento personal', bench: 'Referencia', you2: 'Tu portafolio',
        returnsNo: 'Aún no hay historial suficiente para medir tu rendimiento.', returnsPrices: 'Faltan precios diarios de {x}; por eso el rendimiento no se sabe.',
        returnsLeft: 'Sin contar {x}: faltan precios diarios.', returnsTable: 'Portafolio contra referencia', review: 'Revisión trimestral',
        flowsMissing: 'Faltan los depósitos y retiros entre estados de cuenta.', upload: 'Sube un estado de cuenta',
        knows: 'Lo que sé de ti', factsN: '{n} datos', dueOne: '1 cifra por confirmar.', dueN: '{n} cifras por confirmar.', reviewNow: 'Revisar',
        passOf: '{i} de {n}', keepGo: 'Seguir', later: 'Después', passDone: 'Todo al día.', historySource: 'Historial y fuente', moreOptions: 'Más opciones',
        g_money_in: 'Dinero que entra', g_money_out: 'Lo que gastas', g_own: 'Lo que tienes', g_owe: 'Lo que debes',
        g_goals: 'Metas', g_invest: 'Cómo inviertes', g_about: 'Sobre ti',
        unknownHead: 'Aún no sé…', monthsShort: '{n} meses de movimientos', leftLine: 'Quedan {x} sin asignar', shortLine: 'Te faltan {x}', avgLine: 'prom. {x} al mes',
        estate: 'Herencia', estateScore: '{n}% completo', estateClear: 'Sin pendientes en lo que me has contado.',
        monthDetail: 'Tu mes en detalle', inAssetsShort: 'en activos', accountsSum: '{n} cuentas', accountsSumUnknown: '{n} cuentas · {u} sin cifra',
        sincePeriod: 'desde el {d}', paidBy: 'liquidas en {d}', fundedUnknown: 'Aún no hay una cuenta ligada a esta meta; por eso no sé el avance.', st_next: 'Próxima',
        connsSum: '{n} conectadas · {m} sin conectar', connsNone: 'Nada conectado aún · estados de cuenta por archivo', staleN: '{n} por actualizar',
        save: 'Guardar', cancel: 'Cancelar', forget: 'Olvidar', close: 'Cerrar', amount: 'Monto', currency: 'Moneda',
        months: 'meses', saved: 'Guardado.', confirmed: 'Gracias, anotado.', undo: 'Deshacer', forgetting: 'Lo voy a olvidar.',
        emptyLine: 'Todavía no sé nada de tu dinero.', tell: 'Cuéntame en el chat',
        more: '{n} cosas más',
       
       
       
       
        forgottenOn: 'Olvidado', replacedOn: 'Reemplazado por un estado de cuenta', now: 'hoy', source: 'Fuente', recorded: 'Registrado', status: 'Estado',
       
        src_you_said: 'Tú me lo dijiste', src_document: 'Estado de cuenta', src_web: 'Web', src_derived: 'Calculado',
       
        writesOff: 'Aún no se puede editar aquí. Díselo a Wealth en el chat.', conflict: 'Tu perfil cambió. Se recargó; inténtalo de nuevo.',
       
        todayDay: 'hoy', tomorrow: 'mañana',
        conns: 'Conexiones y datos', keyKeychain: 'Llave en el llavero', keyEnv: 'Llave en el entorno', noKey: 'Sin conectar',
        cap_ibkr_flex: 'Solo lectura: posiciones, operaciones, dividendos y efectivo', cap_alpaca: 'Solo lectura: posiciones, movimientos y dividendos',
        cap_cuenca: 'Solo lectura: saldo, movimientos y apartados', cap_statements: 'Solo lectura: lo que traiga el PDF o CSV',
        statements: 'Estados de cuenta', uploadRow: 'Subir estado de cuenta', synced: 'Sincronizado {t}',
        sync: 'Sincronizar', howTo: 'Cómo conectar', unknownS: 'Desconocido',
        syncAsk: 'Sincroniza mi cuenta de {i} (solo lectura) y muéstrame el resumen antes de guardar nada.',
        syncAskIbkr: ' Mi query id de Flex es ', uploadAsk: 'Te adjunto un estado de cuenta.',
        sheetTitle: 'Conectar {i}', step1_ibkr_flex: 'En el portal de IBKR crea una Activity Flex Query (XML, últimos 365 días); luego activa Flex Web Service y copia el token vigente.',
        step1_alpaca: 'En el panel de Alpaca genera una API key y copia el key id y el secret (el secret se muestra una sola vez).',
        step1_cuenca: 'Pide a Cuenca una API key y secret para tu cuenta. Sin llave, sube el estado de cuenta mensual.',
        step2: 'Guárdala tú desde la Terminal; macOS te pide el valor. Nunca la pegues en el chat.',
        step3: 'O, solo para esta sesión, exporta:', step4: 'Luego toca Sincronizar aquí.', step4_ibkr_flex: 'Luego toca Sincronizar aquí y ten a la mano tu query id.',
        readOnlyLine: 'Wealth solo lee. No puede operar ni mover dinero.', copy: 'Copiar', copied: 'Copiado.',
        taxPack: 'Paquete fiscal {y} para tu contador (imprimible)', taxPackFailed: 'No se pudo descargar el paquete fiscal.',
        export: 'Descargar todo (JSON)', erase: 'Borrar mis datos', eraseLine: 'Solo desde la Terminal, para que nada lo haga por error.', exportFailed: 'No se pudo descargar la exportación.',
        date: 'Fecha', item: 'Concepto', value: 'Valor', share: 'Proporción',
      }
    };
    const root = document.getElementById('root');
    const sheet = document.getElementById('sheet');
    const toast = document.getElementById('toast');
    let lang = 'en', locale = 'en-US', data = null, token = '', writes = true, toastTimer = null;
    let todayData = null, conns = null;   // the Hoy lines and the Conexiones rows (token-protected reads)
    let editing = null;            // id of the sentence being edited in place
    let memoryOpen = false;        // "What I know about you" starts collapsed
    let period = null;             // the returns period on show
    const hidden = new Set();      // sentences forgotten but still undoable
    let pending = null;            // { item, timer } — the delete waiting out its undo window

    // ------------------------------------------------------------ helpers (DOM only; never markup from data)
    function t(key, vars) {
      const text = (I18N[lang] && I18N[lang][key]) ?? I18N.en[key] ?? key;
      return vars && typeof text === 'string' ? text.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? '')) : text;
    }
    const has = key => Object.prototype.hasOwnProperty.call(I18N.en, key);
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
      return append(el, kids);
    }
    function append(el, kids) {
      for (const kid of kids.flat(Infinity)) {
        if (kid === null || kid === undefined || kid === false || kid === '') continue;
        el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
      }
      return el;
    }
    const NS = 'http://www.w3.org/2000/svg';
    function s(tag, attrs, ...kids) {
      const el = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, String(v));
      kids.flat().forEach(kid => kid && el.append(kid));
      return el;
    }
    function fill(el, ...kids) { el.replaceChildren(); return append(el, kids); }
    const isNum = v => typeof v === 'number' && Number.isFinite(v);
    const minus = text => text.replace(/-/g, '−');
    function money(amount, currency, opts = {}) {
      if (!isNum(amount)) return null;
      const digits = Math.abs(amount) >= 1000 || Number.isInteger(amount) ? 0 : 2;
      let text;
      try {
        text = new Intl.NumberFormat(locale, { style: 'currency', currency: currency || 'XXX', currencyDisplay: 'narrowSymbol',
          maximumFractionDigits: opts.whole ? 0 : digits, minimumFractionDigits: 0, notation: opts.compact ? 'compact' : 'standard' }).format(amount);
      } catch (_) { text = new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(amount); }
      return minus(currency && !opts.bare ? `${text} ${currency}` : text);
    }
    const cur = () => (data.picture && data.picture.currency) || data.reporting_currency;
    // In the reporting currency the code is implied; any other currency always says its code.
    const m = (amount, currency, opts = {}) => money(amount, currency || cur(), { whole: true, ...opts, bare: !currency || currency === cur() });
    const num = (v, d = 0) => isNum(v) ? minus(new Intl.NumberFormat(locale, { maximumFractionDigits: d }).format(v)) : null;
    const pct = (v, signed, d = 1) => isNum(v) ? minus(new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: d, signDisplay: signed ? 'exceptZero' : 'auto' }).format(v)) : null;
    const asDate = iso => { const d = new Date(String(iso).slice(0, 10) + 'T00:00:00Z'); return Number.isNaN(d.getTime()) ? null : d; };
    function day(iso) { const d = asDate(iso); return d ? new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeZone: 'UTC' }).format(d) : ''; }
    function monthYear(iso, style = 'short') { const d = asDate(String(iso).length === 7 ? iso + '-15' : iso); return d ? new Intl.DateTimeFormat(locale, { month: style, year: 'numeric', timeZone: 'UTC' }).format(d).replace('.', '') : ''; }
    function monthOnly(iso) { const d = asDate(iso + '-15'); return d ? new Intl.DateTimeFormat(locale, { month: 'short', timeZone: 'UTC' }).format(d).replace('.', '') : ''; }
    function short(iso) {
      const d = asDate(iso), now = asDate(data?.today);
      if (!d) return '';
      const sameYear = now && d.getUTCFullYear() === now.getUTCFullYear();
      return new Intl.DateTimeFormat(locale, { day: 'numeric', month: lang === 'es' ? 'long' : 'short', ...(sameYear ? {} : { year: 'numeric' }), timeZone: 'UTC' }).format(d);
    }
    function railDate(iso) {
      const d = asDate(iso), now = asDate(data?.today);
      if (!d) return '';
      const sameYear = now && d.getUTCFullYear() === now.getUTCFullYear();
      return new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'short', ...(sameYear ? {} : { year: 'numeric' }), timeZone: 'UTC' }).format(d).replace('.', '');
    }
    const list = items => {
      const parts = items.filter(Boolean).map(String);
      if (parts.length < 2) return parts[0] || '';
      try { return new Intl.ListFormat(locale, { style: 'long', type: 'conjunction' }).format(parts); } catch (_) { return parts.join(', '); }
    };
    const pair = v => (v && typeof v === 'object') ? String(v[lang] || v.en || '') : String(v ?? '');
    // A sentence from the server, its values set in weight (never colour).
    function sentence(text, spans) {
      const out = [];
      let at = 0;
      for (const [a, b] of spans || []) {
        if (a > at) out.push(text.slice(at, a));
        out.push(h('span', { class: 'v', text: text.slice(a, b) }));
        at = b;
      }
      out.push(text.slice(at));
      return out;
    }
    function say(msg, action) {
      clearTimeout(toastTimer);
      fill(toast, h('p', { text: msg }), action || null);
      toastTimer = setTimeout(() => toast.replaceChildren(), action ? 5200 : 3200);
    }
    // Every chart carries its data as a table for screen readers; the drawing itself is aria-hidden.
    function srTable(caption, head, rows) {
      // The table sits in a 1px clipped box: a table never shrinks to its own width, a div does.
      return h('div', { class: 'sr' }, h('table', {}, h('caption', { text: caption }),
        h('thead', {}, h('tr', {}, head.map(c => h('th', { scope: 'col', text: c })))),
        h('tbody', {}, rows.map(r => h('tr', {}, r.map((c, i) => i ? h('td', { text: c ?? '—' }) : h('th', { scope: 'row', text: c ?? '—' })))))));
    }
    function chart(caption, visual, head, rows, extra) {
      return h('figure', { class: 'chart', 'aria-label': caption }, h('div', { 'aria-hidden': 'true' }, visual), srTable(caption, head, rows), extra || null);
    }
    function hatch(id) {
      // One hatch texture means "owed" everywhere; drawn in pixels, so it never stretches.
      const svg = s('svg', { 'aria-hidden': 'true' },
        s('defs', {}, s('pattern', { id, width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
          s('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: 'var(--tone-2)', 'stroke-width': 2 }))),
        s('rect', { width: '100%', height: '100%', fill: `url(#${id})` }));
      return svg;
    }
    let hatchN = 0;
    function swatch(tone) {
      const el = h('span', { class: 'sw ' + tone, 'aria-hidden': 'true' });
      if (tone === 'hatch') el.append(hatch('hs' + (++hatchN)));
      return el;
    }
    // A stacked bar on one baseline: [{ tone, value, title }] as flex parts with 2px surface gaps.
    function stack(parts, total) {
      const sum = total || parts.reduce((a, p) => a + Math.max(0, p.value), 0) || 1;
      return h('div', { class: 'stack' }, parts.filter(p => p.value > 0).map(p => {
        const el = h('span', { class: p.tone, title: p.title || null, style: { flex: `${p.value / sum} 1 0` } });
        if (p.tone === 'hatch') el.append(hatch('hb' + (++hatchN)));
        return el;
      }));
    }
    function sec(id, title, meta, ...body) {
      return h('section', { class: 'sec', 'aria-labelledby': id + '-h', id },
        h('div', { class: 'sec-h' }, h('h2', { id: id + '-h', text: title }), meta || null), body);
    }
    const metaLine = text => text ? h('span', { class: 'sec-meta', text }) : null;
    const tellBtn = (prefill, label) => h('button', { class: 'txt', type: 'button', text: label || t('tellMe'), onclick: () => handoff(prefill, false) });

    // ------------------------------------------------------------ network
    async function load() {
      try {
        const response = await fetch('/api/profile?lang=' + lang, { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Wealth-Token': token } });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || t('loadError'));
        data = body;
        if (data.writes === false) writes = false;
        await loadSurfaces();
        render();
      } catch (error) {
        fill(root, h('p', { class: 'say', style: { marginTop: '40px' }, text: error instanceof Error ? error.message : t('loadError') }));
      } finally { root.setAttribute('aria-busy', 'false'); }
    }
    async function post(path, payload) {
      if (!writes) { say(t('writesOff')); return false; }
      const response = await fetch(path, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Wealth-Token': token },
        body: JSON.stringify({ ...payload, expected_revision: data?.client?.revision ?? null })
      }).catch(() => null);
      if (!response) { say(t('loadError')); return false; }
      const body = await response.json().catch(() => ({}));
      if ([404, 405, 501].includes(response.status)) { writes = false; if (sheet.open) sheet.close(); render(); say(t('writesOff')); return false; }
      if (response.status === 409) { editing = null; if (sheet.open) sheet.close(); await load(); say(t('conflict')); return false; }
      if (!response.ok) return body.error || String(response.status);
      if (body?.profile?.version) data = body.profile; else await load();
      return true;
    }
    const factPath = key => '/api/facts/' + encodeURIComponent(key);
    async function act(key, action, field, value) {
      const result = await post(factPath(key), { action, field: field ?? null, value });
      if (result === true) {
        editing = null;
        if (sheet.open) sheet.close();
        render();
        if (action !== 'delete') say(action === 'confirm' ? t('confirmed') : t('saved'));
      }
      return result;
    }
    const editValue = (spec, amount, currency) => spec.kind === 'number' ? Number(amount)
      : spec.wrap ? { [spec.wrap]: amount, currency } : { amount, currency, ...(spec.period ? { period: spec.period } : {}) };

    // ------------------------------------------------------------ forget, with a five-second undo
    function forget(item) {
      if (!writes) { say(t('writesOff')); return; }
      if (pending) commitForget();
      hidden.add(item.text);
      editing = null;
      if (sheet.open) sheet.close();
      render();
      const undo = h('button', { class: 'txt', type: 'button', text: t('undo'), onclick: () => {
        clearTimeout(pending?.timer); pending = null; hidden.delete(item.text); toast.replaceChildren(); render();
        document.querySelector(`[data-say="${CSS.escape(item.id)}"]`)?.focus();
      } });
      say(t('forgetting'), undo);
      pending = { item, timer: setTimeout(commitForget, 5000) };
    }
    async function commitForget() {
      if (!pending) return;
      const { item, timer } = pending;
      clearTimeout(timer);
      pending = null;
      const result = await post(factPath(item.key), { action: 'delete', field: item.forget?.field ?? null });
      hidden.delete(item.text);
      render();
      if (result !== true && result !== false) say(String(result));
    }
    window.addEventListener('pagehide', () => { if (pending && writes) {
      fetch(factPath(pending.item.key), { method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: { 'Content-Type': 'application/json', 'X-Wealth-Token': token },
        body: JSON.stringify({ action: 'delete', field: pending.item.forget?.field ?? null,
          expected_revision: data?.client?.revision ?? null }) }).catch(() => {});
    } });

    // ------------------------------------------------------------ inline edit: tap the sentence, change the figure
    function currencies(current) {
      return [...new Set([current, data.reporting_currency, 'MXN', 'USD', 'EUR', 'CAD'].filter(c => typeof c === 'string' && /^[A-Z]{3}$/.test(c)))];
    }
    const parseAmount = text => {
      const cleaned = String(text).replace(/[^\d.,-]/g, '');
      // es-MX and en both group with commas; a lone comma followed by 1-2 digits is a decimal.
      const value = /^\d+,\d{1,2}$/.test(cleaned) ? Number(cleaned.replace(',', '.')) : Number(cleaned.replace(/,/g, ''));
      return String(text).trim() === '' || !Number.isFinite(value) || value < 0 ? null : value;
    };
    function amountField(spec) {
      const input = h('input', { type: 'text', inputmode: 'decimal', autocomplete: 'off', 'aria-label': t('amount'),
        value: isNum(spec.amount) ? new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(spec.amount) : '' });
      const ccy = spec.kind === 'amount' ? h('select', { 'aria-label': t('currency') },
        currencies(spec.currency).map(c => h('option', { value: c, text: c, selected: c === (spec.currency || data.reporting_currency) }))) : null;
      const row = h('div', { class: 'amount-row' },
        h('label', { class: 'money-input' }, spec.kind === 'amount' ? h('span', { 'aria-hidden': 'true', text: '$' }) : null, input,
          spec.unit === 'months' ? h('span', { class: 'unit', text: t('months') }) : null),
        ccy);
      return { row, input, ccy };
    }
    // History, source and forget sit behind one quiet "⋯" on the editor, never on every row.
    function overflow(item) {
      const menu = h('div', { class: 'more-menu', hidden: true },
        item.key ? h('button', { class: 'txt small', type: 'button', text: t('historySource'), onclick: () => openSheet(item) }) : null,
        item.forget && writes ? h('button', { class: 'txt small', type: 'button', text: t('forget'), onclick: () => forget(item) }) : null);
      const toggle = h('button', { class: 'more', type: 'button', 'aria-label': t('moreOptions'), 'aria-expanded': 'false', text: '⋯',
        onclick: () => { menu.hidden = !menu.hidden; toggle.setAttribute('aria-expanded', String(!menu.hidden)); } });
      return [toggle, menu];
    }
    function editor(item, spec) {
      const err = h('p', { class: 'err', role: 'alert' });
      const { row, input, ccy } = amountField(spec);
      const close = () => { editing = null; render(); document.querySelector(`[data-say="${CSS.escape(item.id)}"]`)?.focus(); };
      const [more, menu] = overflow(item);
      const form = h('form', { class: 'edit', 'aria-label': item.text,
        onsubmit: async e => {
          e.preventDefault();
          const amount = parseAmount(input.value);
          if (amount === null) { err.textContent = t('amount'); input.focus(); return; }
          const result = await act(item.key, 'edit', spec.field, editValue(spec, amount, ccy?.value));
          if (typeof result === 'string') err.textContent = result;
        } },
        row, err,
        h('div', { class: 'acts' },
          h('button', { class: 'btn', type: 'submit', text: t('save') }),
          h('button', { class: 'txt', type: 'button', text: t('cancel'), onclick: close }),
          h('span', { class: 'push' }, more)),
        menu);
      form.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); close(); } });
      requestAnimationFrame(() => { input.focus(); input.select(); });
      return form;
    }

    // ------------------------------------------------------------ history and source (fetched on tap)
    const read = path => fetch(path, { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Wealth-Token': token } })
      .then(r => r.ok ? r.json() : null).catch(() => null);
    async function openSheet(item) {
      if (!item.key) return;
      const [detail, history] = await Promise.all([read(factPath(item.key)),
        read('/api/profile/fact/' + encodeURIComponent(item.key) + '/history')]);
      const periods = (history?.entries || []).filter(e => e && e.valid_from).slice(0, 5);
      const periodText = e => e.status === 'forgotten' ? `${t('forgottenOn')} · ${short(e.valid_from)}`
        : e.status === 'replaced' ? `${t('replacedOn')} · ${short(e.valid_from)}`
        : e.valid_to ? `${monthYear(e.valid_from)} – ${monthYear(e.valid_to)}` : `${monthYear(e.valid_from)} – ${t('now')}`;
      const worth = e => {
        const v = e.value;
        if (v && typeof v === 'object') for (const f of ['amount', 'balance', 'total', 'value'])
          if (isNum(v[f]) && typeof v.currency === 'string') return money(v[f], v.currency);
        return e.status === 'forgotten' || e.status === 'replaced' ? '' : String(e.text || '').replace(/ (since|from) .*$/, '');
      };
      const line = (k, v, mono) => h('div', { class: 'line' }, h('span', { class: 'k', text: k }),
        h('span', { class: 'v', style: mono ? null : { fontFamily: 'var(--sans)' }, text: v }));
      const src = detail?.source || {};
      fill(sheet,
        h('button', { class: 'txt close', type: 'button', text: t('close'), onclick: () => sheet.close() }),
        h('p', { class: 'say', id: 'sheet-title' }, sentence(item.text, item.emphasis)),
        periods.length > 1 ? h('div', { class: 'ticket' }, periods.map(e => line(periodText(e), worth(e), true))) : null,
        h('div', { class: 'ticket', style: { marginTop: periods.length > 1 ? '24px' : '0' } },
          line(t('source'), [t('src_' + (src.label || 'derived')), src.ref && src.label === 'document' ? src.ref : null].filter(Boolean).join(' · ')),
          src.observed_on ? line(t('recorded'), day(src.observed_on), true) : null),
        item.forget && writes ? h('div', { class: 'acts' }, h('button', { class: 'txt', type: 'button', text: t('forget'), onclick: () => forget(item) })) : null);
      sheet.dataset.from = item.id;
      if (!sheet.open) sheet.showModal();
    }
    sheet.addEventListener('close', () => {
      const id = sheet.dataset.from;
      if (pass) { pass = null; render(); }
      if (id) document.querySelector(`[data-say="${CSS.escape(id)}"]`)?.focus();
    });

    // ------------------------------------------------------------ one focused pass over what needs confirming
    // Stale figures, guesses and statement-vs-said differences, one at a time: Enter keeps the figure shown,
    // typing a new one changes it. Nothing on the page asks row by row.
    let pass = null;   // { items, at }
    function passQueue(mem) {
      const seen = new Set(), out = [];
      const add = (kind, item) => { if (!seen.has(item.id) && !hidden.has(item.text)) { seen.add(item.id); out.push({ kind, item }); } };
      (mem.conflicts || []).forEach(c => add('conflict', { ...c, id: 'conflict-' + c.id }));
      (mem.review || []).forEach(i => add('review', i));
      mem.groups.forEach(g => g.facts.forEach(f => { if (f.confirm && f.origin?.kind === 'guess') add('review', f); }));
      return out;
    }
    function openPass() {
      pass = { items: passQueue(memory()), at: 0 };
      if (!sheet.open) sheet.showModal();
      drawPass();  // after opening, so the figure (not Close) has the focus
    }
    async function contradiction(c, choice) {
      const response = await fetch('/api/profile/contradictions/' + encodeURIComponent(c.contradiction_id), {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Wealth-Token': token },
        body: JSON.stringify({ choice, ...(choice === 'changed' && c.as_of ? { valid_from: c.as_of } : {}), expected_revision: data?.client?.revision ?? null })
      }).catch(() => null);
      if (!response || !response.ok) return false;
      const body = await response.json().catch(() => ({}));
      if (body?.profile?.version) data = body.profile; else await load();
      return true;
    }
    async function passAnswer(entry, amount, currency) {
      const { kind, item } = entry;
      const spec = item.edit;
      const changed = amount !== null && spec && (amount !== spec.amount || (currency && currency !== spec.currency));
      if (!changed) {
        if (kind === 'conflict' && item.contradiction_id) return contradiction(item, 'keep');
        return (await post(factPath(item.key), { action: 'confirm', field: spec?.field ?? null })) === true;
      }
      if (kind === 'conflict' && item.contradiction_id) {
        const statement = item.use_statement;
        if (statement && amount === statement.amount) return contradiction(item, 'use_new');
        if (!(await contradiction(item, 'changed'))) return false;
      }
      return (await post(factPath(item.key), { action: 'edit', field: spec.field, value: editValue(spec, amount, currency) })) === true;
    }
    function drawPass() {
      const entry = pass && pass.items[pass.at];
      if (!entry) { pass = null; sheet.close(); render(); say(t('passDone')); return; }
      const { item } = entry;
      const err = h('p', { class: 'err', role: 'alert' });
      const field = item.edit ? amountField(item.edit) : null;
      const next = () => { pass.at += 1; drawPass(); };
      const form = h('form', { class: 'edit', onsubmit: async e => {
        e.preventDefault();
        const amount = field ? parseAmount(field.input.value) : null;
        if (field && amount === null) { err.textContent = t('amount'); field.input.focus(); return; }
        const ok = await passAnswer(entry, amount, field?.ccy?.value);
        if (!pass) return;  // a conflict reloaded the page and closed the pass
        if (ok) next(); else err.textContent = t('loadError');
      } },
        field ? field.row : null, err,
        h('div', { class: 'acts' },
          h('button', { class: 'btn', type: 'submit', text: t('keepGo') }),
          h('button', { class: 'txt', type: 'button', text: t('later'), onclick: next })));
      fill(sheet,
        h('button', { class: 'txt close', type: 'button', text: t('close'), onclick: () => sheet.close() }),
        h('p', { class: 'origin', text: t('passOf', { i: num(pass.at + 1), n: num(pass.items.length) }) }),
        h('p', { class: 'say', id: 'sheet-title', style: { marginTop: '8px' } }, sentence(item.text, item.emphasis)),
        form);
      sheet.dataset.from = '';
      if (field) { field.input.focus(); field.input.select(); } else form.querySelector('.btn')?.focus();
    }

    // ------------------------------------------------------------ render
    const memory = () => (data.memory && data.memory[lang]) || { groups: [], conflicts: [], review: [], missing: [] };
    const openDiscs = new Set();   // disclosures the person opened stay open across re-renders
    const reduced = () => { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (_) { return true; } };
    // A ruled line that says the answer; the detail opens under it and stays open while the page re-renders.
    function disc(id, title, summary, ...body) {
      const box = h('details', { class: 'disc', id, open: openDiscs.has(id) || null },
        h('summary', {}, h('h2', { text: title }), summary ? h('span', { class: 'sum' }, summary) : null, h('span', { class: 'chev', 'aria-hidden': 'true', text: '⌄' })),
        h('div', { class: 'disc-body' }, body));
      box.addEventListener('toggle', () => { if (box.open) openDiscs.add(id); else openDiscs.delete(id); });
      return box;
    }
    function render() {
      document.title = `${t('you')} · Wealth`;
      const ov = data.overview || { status: 'empty' };
      const p = data.picture || {};
      const mem = memory();
      const empty = ov.status === 'empty' && !(p.worth && (isNum(p.worth.total) || isNum(p.worth.known_total)))
        && !mem.groups.length && !mem.conflicts.length && !mem.review.length;
      if (empty) {
        fill(root, h('h1', {}, t('you'), h('span', { class: 'dot', text: '.' })), renderEmpty(),
          h('div', { class: 'board' }, h('div', { class: 'span' }, renderConnections())));
        return;
      }
      // Above the fold: the hero, Hoy and three tiles. Below: one ruled line per area, each a disclosure.
      const tiles = [renderMonthTile(p), renderGoalsTile(p), renderReserveTile(p)].filter(Boolean);
      const unknowns = renderUnknowns(p);
      const left = [renderMonthDetail(p), renderHave(p)].filter(Boolean);
      const right = [renderOwe(p), renderReturns(p), renderEstate()].filter(Boolean);
      fill(root,
        h('h1', {}, t('you'), h('span', { class: 'dot', text: '.' })),
        h('div', { class: 'board' },
          h('div', { class: 'span' }, renderHero(p)),
          renderToday() ? h('div', { class: 'span' }, renderToday()) : null,
          tiles.length ? h('div', { class: 'span tiles' }, tiles) : null,
          unknowns ? h('div', { class: 'span' }, unknowns) : null,
          left.length ? h('div', { class: 'col' }, left) : null,
          right.length ? h('div', { class: 'col' }, right) : null,
          h('div', { class: 'span' }, renderMemory(mem)),
          h('div', { class: 'span' }, renderConnections())));
    }

    function renderEmpty() {
      return h('section', { class: 'lead' }, h('p', { class: 'say', text: t('emptyLine') }),
        h('div', { class: 'acts' }, h('a', { class: 'btn', href: '/?draft=statement', text: t('upload') }),
          h('a', { class: 'txt', href: '/?draft=accounts', text: t('tell') })));
    }

    // ---- hero: one number, the arithmetic it comes from, and how it moved
    function renderHero(p) {
      const w = p.worth || {};
      const value = isNum(w.total) ? w.total : w.known_total;
      const without = (w.without || []).map(String);
      const hero = h('section', { class: 'hero', 'aria-labelledby': 'worth-h' });
      const main = h('div', {},
        h('p', { class: 'kicker', id: 'worth-h' }, t('netWorth'), w.as_of ? [' · ', h('span', { class: 'num', text: t('asOfK', { d: railDate(w.as_of) }) })] : ''),
        h('p', { class: 'figure' }, isNum(value) ? m(value) : '—', h('span', { class: 'ccy', text: w.currency || '' })),
        without.length && isNum(value) ? h('p', { class: 'qual' }, t('without', { x: '' }), h('span', { class: 'v', text: list(without) })) : null,
        !isNum(value) && without.length ? h('p', { class: 'qual', text: t('unknownWorth', { x: list(without) }) }) : null);
      hero.append(main);
      const hist = p.history;
      if (hist && (hist.points || []).length >= 2) hero.append(sparkline(hist));
      const parts = [
        { id: 'liquid', tone: 't1', value: w.liquid, label: t('liquid') },
        { id: 'illiquid', tone: 't3', value: w.illiquid, label: t('illiquid') },
        { id: 'debts', tone: 'hatch', value: w.debts, label: t('debts'), neg: true }].filter(x => isNum(x.value) && x.value > 0);
      if (parts.length && isNum(value)) {
        const total = parts.reduce((a, x) => a + x.value, 0);
        // The ledger line: Líquido − Deudas = Patrimonio. The hero figure is the sum of what is drawn above it.
        const term = (label, amount, tone, cls) => h('span', { class: 'term' + (cls ? ' ' + cls : '') }, tone ? swatch(tone) : null, label, h('span', { class: 'num', text: m(amount) }));
        const ledger = h('div', { class: 'ledger' });
        parts.forEach((x, i) => {
          if (i) ledger.append(h('span', { class: 'op', 'aria-hidden': 'true', text: x.neg ? '−' : '+' }));
          ledger.append(term(x.label, x.value, x.tone));
        });
        ledger.append(h('span', { class: 'op', 'aria-hidden': 'true', text: '=' }), term(t('netWorth'), value, null, 'total'));
        const visual = h('div', {}, stack(parts.map(x => ({ tone: x.tone, value: x.value, title: `${x.label}: ${m(x.neg ? -x.value : x.value)}` })), total), ledger);
        hero.append(h('div', { class: 'worth-bar' }, chart(t('worthTable'), visual, [t('item'), t('value')],
          [...parts.map(x => [x.label, m(x.neg ? -x.value : x.value)]), [t('netWorth'), m(value)]])));
      }
      return hero;
    }
    // The delta counts up on load (unless motion is reduced); cobalt marks the end only when the last point is today.
    function countUp(node, target, format) {
      if (reduced() || !isNum(target) || Math.abs(target) < 1) { node.textContent = format(target); return; }
      const start = performance.now(), ms = 900;
      const step = now => {
        const k = Math.min(1, (now - start) / ms), eased = 1 - Math.pow(1 - k, 3);
        node.textContent = format(target * eased);
        if (k < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }
    function sparkline(hist) {
      const pts = hist.points.filter(pt => isNum(pt.v));
      const ys = pts.map(pt => pt.v);
      let lo = Math.min(...ys), hi = Math.max(...ys);
      const pad = (hi - lo) * 0.1 || Math.abs(hi) * 0.05 || 1; lo -= pad; hi += pad;
      const X = i => i / (pts.length - 1) * 1000, Y = v => 1000 - (v - lo) / (hi - lo) * 1000;
      const plot = h('div', { class: 'spark' });
      plot.append(s('svg', { viewBox: '0 0 1000 1000', preserveAspectRatio: 'none' },
        s('polyline', { points: ys.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' '), fill: 'none', stroke: 'var(--ink)',
          'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' })));
      const today = String(data?.today || '').slice(0, 10), lastDay = String(pts[pts.length - 1].d || '').slice(0, 10);
      plot.append(h('span', { class: 'end-dot' + (today && lastDay === today ? ' live' : ''), style: { left: '100%', top: (Y(ys[ys.length - 1]) / 10).toFixed(2) + '%' } }));
      const change = ys[ys.length - 1] - ys[0];
      const delta = h('span', { class: 'v' });
      countUp(delta, change, v => (v >= 0 ? '+' : '') + m(Math.round(v)));
      const caption = t('history', { x: list(hist.accounts || []), p: pct(hist.coverage, false, 0) || '—' });
      const visual = h('div', {}, plot,
        h('div', { class: 'trend-line' },
          h('span', {}, delta, ' ', t('since', { d: railDate(pts[0].d) })),
          h('span', { text: lastDay === today ? t('nowK') : railDate(lastDay) })));
      return h('div', { class: 'trend' }, chart(caption, visual, [t('date'), t('value')], pts.map(pt => [railDate(pt.d), m(pt.v)])),
        h('p', { class: 'note', text: caption }));
    }

    // ---- Hoy: at most three things worth doing, each one tap into the chat
    function dueText(item) {
      if (!item.due || typeof item.days !== 'number') return null;
      return item.days === 0 ? t('todayDay') : item.days === 1 ? t('tomorrow') : railDate(item.due);
    }
    // The value to set in weight. The server's span is a hint; the figure it lands on is what gets bold, whole.
    function hoyEmphasis(title, span) {
      let [a, b] = Array.isArray(span) ? span.map(Number) : [0, 0];
      if (!(b > a && a >= 0 && b <= title.length)) return null;
      const at = i => (i >= 0 && i < title.length ? title[i] : ' ');
      const digit = i => /\d/.test(at(i));
      const inNumber = i => digit(i) || (/[.,]/.test(at(i)) && digit(i - 1) && digit(i + 1));
      const word = i => /[\p{L}\p{N}]/u.test(at(i));
      while (a < b && /\s/.test(at(a))) a += 1;
      while (b > a && /\s/.test(at(b - 1))) b -= 1;
      // A start inside a number backs up to its first digit; a start inside a word skips to the next word.
      if (inNumber(a) && inNumber(a - 1)) { while (inNumber(a - 1)) a -= 1; }
      else if (word(a - 1) && word(a)) { while (a < b && word(a)) a += 1; while (a < b && /\s/.test(at(a))) a += 1; }
      if (/[$€£]/.test(at(a - 1))) a -= 1;
      // An end inside a number or a word runs to its last character; a percent sign belongs to its number.
      if ((inNumber(b - 1) && inNumber(b)) || (word(b - 1) && word(b))) { while (inNumber(b) || word(b)) b += 1; }
      if (at(b) === '%') b += 1;
      return a < b ? [a, b] : null;
    }
    function renderToday() {
      const items = (todayData?.items || []).slice(0, 3);
      if (!items.length) return null;
      return h('section', { class: 'sec', 'aria-labelledby': 'today-h' },
        h('div', { class: 'sec-h' }, h('h2', { id: 'today-h', text: t('today') })),
        h('ul', { class: 'hoy' }, items.map(item => {
          const title = String(item.title?.[lang] || '');
          const span = hoyEmphasis(title, (item.emphasis?.[lang] || [])[0]);
          const due = dueText(item);
          // The date shows once: a title that already names its day carries no tag.
          const named = due && title.toLowerCase().includes(due.toLowerCase());
          return h('li', {}, h('button', { type: 'button', class: 'hoy-go', onclick: () => handoff(String(item.next_step?.[lang] || title), true) },
            h('span', { class: 'mark ' + (['act', 'consider'].includes(item.severity) ? item.severity : ''), 'aria-hidden': 'true' }),
            h('span', { class: 'hoy-title' }, span ? [title.slice(0, span[0]), h('span', { class: 'v', text: title.slice(span[0], span[1]) }), title.slice(span[1])] : title),
            due && !named ? h('time', { class: 'hoy-due', datetime: item.due, text: due }) : null,
            h('span', { class: 'hoy-arrow', 'aria-hidden': 'true', text: '→' })));
        })));
    }

    // ---- What is not known yet: one list, one way forward per line (no empty sections anywhere else)
    function renderUnknowns(p) {
      const rows = [];
      const add = (what, action) => rows.push(h('li', {}, h('span', { class: 'what', text: what }), action || null));
      const f = p.flow || {};
      if (f.status !== 'ready') {
        const miss = f.missing || [];
        add(miss.includes('income') ? t('missIncome') : t('missSpending'), tellBtn(miss.includes('income') ? t('askIncome') : t('askSpending')));
      }
      const missing = data.completeness?.missing || [];
      if (!(p.debts || []).length && missing.includes('debts')) add(t('debtsAsk'), tellBtn(t('askDebts')));
      if (!(p.goals || []).length && missing.includes('goals')) add(t('noGoals'), tellBtn(t('askGoals')));
      const r = p.reserve || {};
      if (isNum(r.months) && !isNum(r.target_months)) add(t('reserveNoTarget'), tellBtn(t('askReserve')));
      const ret = p.returns || {};
      if (ret.status !== 'ready' && (data.performance || {}).status !== 'ready') {
        const line = ret.reason === 'prices' && (ret.left_out || []).length ? t('returnsPrices', { x: list(ret.left_out) })
          : (data.performance || {}).reason === 'flows_unknown' ? t('flowsMissing') : t('returnsNo');
        add(line, (p.accounts || []).length ? h('a', { class: 'txt', href: '/review', text: t('review') }) : null);
      }
      if (!rows.length) return null;
      return sec('unknown', t('unknownHead'), null, h('ul', { class: 'unknowns' }, rows));
    }

    // ---- Tu mes: the tile says the rate and shows the baseline; the detail opens the rows and the months
    const FLOW_TONES = { essentials: 't1', spending: 't1', other: 't2', debt: 'hatch', committed: 't3', unallocated: 'open' };
    function flowParts(f) {
      const segs = f.segments || [];
      const left = segs.find(x => x.id === 'unallocated');
      const short = left && left.value < 0 ? -left.value : 0;
      const parts = segs.filter(x => x.value > 0).map(x => ({ tone: FLOW_TONES[x.id] || 't2', value: x.value, title: `${t('f_' + x.id)}: ${m(x.value)}` }));
      if (short) parts.push({ tone: 'over', value: short, title: `${t('f_short')}: ${m(short)}` });
      return { segs, left, short, parts, scale: f.income + short };
    }
    function renderMonthTile(p) {
      const f = p.flow || {};
      if (f.status !== 'ready') return null;
      const { left, short, parts, scale } = flowParts(f);
      const visual = h('div', { class: 'flow' },
        h('div', { class: 'flow-in', style: { width: (f.income / scale * 100).toFixed(2) + '%' } }, h('span', { text: t('incomeIn') }), h('span', { class: 'num', text: m(f.income) })),
        h('div', { class: 'flow-rule', style: { width: (f.income / scale * 100).toFixed(2) + '%' } }),
        stack(parts, scale));
      const rateFig = isNum(f.savings_rate) ? h('div', { class: 'rate' }, h('span', { class: 'figure-s num', text: pct(f.savings_rate, false, 0) }), h('span', { class: 'quiet', text: t('savingsRate') })) : null;
      const line = short ? t('shortLine', { x: m(short) }) : left && left.value > 0 ? t('leftLine', { x: m(left.value) }) : null;
      return h('section', { class: 'tile', 'aria-labelledby': 'month-h' }, h('h2', { id: 'month-h', text: t('month') }), rateFig,
        chart(t('month'), visual, [t('item'), t('value')], [[t('incomeIn'), m(f.income)], ...parts.map(x => [x.title.split(':')[0], m(x.value)])]),
        line ? h('p', { class: 'note', text: line }) : null);
    }
    function renderMonthDetail(p) {
      const f = p.flow || {};
      if (f.status !== 'ready') return null;
      const { segs } = flowParts(f);
      const meta = f.source === 'ledger' && f.months ? t('monthsShort', { n: f.months }) : t('stated');
      const rows = segs.filter(x => x.value !== 0);
      const legend = h('ul', { class: 'rows' }, rows.map(x => h('li', {},
        swatch(x.value < 0 ? 'open' : FLOW_TONES[x.id] || 't2'),
        h('span', { class: 'lbl', text: t(x.value < 0 ? 'f_short' : 'f_' + x.id) }),
        h('span', { class: 'val' }, h('span', { class: 'num', text: m(Math.abs(x.value)) }), h('span', { class: 'share num', text: pct(Math.abs(x.value) / f.income) })))));
      const sp = p.spending;
      const summary = [sp && isNum(sp.average) ? t('avgLine', { x: m(sp.average) }) : null, meta].filter(Boolean).join(' · ');
      return disc('month-detail', t('monthDetail'), summary,
        chart(t('month'), legend, [t('item'), t('value'), t('ofIncome')], [[t('incomeIn'), m(f.income), '100%'], ...rows.map(x => [t(x.value < 0 ? 'f_short' : 'f_' + x.id), m(Math.abs(x.value)), pct(Math.abs(x.value) / f.income)])]),
        (f.debt_unknown || []).length ? h('p', { class: 'note', text: t('debtUnknown', { x: list((f.debt_unknown || []).map(pair)) }) }) : null,
        renderSpending(sp));
    }
    function renderSpending(sp) {
      if (!sp || (sp.months || []).length < 2) return null;
      const months = sp.months.filter(x => isNum(x.total)).slice(-12);
      const top = Math.max(...months.map(x => x.total), sp.average || 0) * 1.1 || 1;
      const bars = h('div', { class: 'cols-bars' },
        months.map((x, i) => h('div', { class: 'colb', title: `${monthYear(x.month)}: ${m(x.total)}` },
          h('span', { class: 'bar' + (i === months.length - 1 ? '' : ' dim'), style: { height: (x.total / top * 100).toFixed(2) + '%' } }))),
        isNum(sp.average) ? h('div', { class: 'avg', style: { bottom: (sp.average / top * 100).toFixed(2) + '%' } }) : null);
      const axis = h('div', { class: 'cols-x' }, months.map(x => h('span', { text: monthOnly(x.month) })));
      const cats = (sp.top || []).map(c => h('li', {},
        h('div', { class: 'bl-line' }, h('span', { class: 'lbl', text: pair(c.label) }),
          h('span', {}, h('span', { class: 'num', text: m(c.value) }), h('span', { class: 'share num', text: pct(c.share, false, 0) }))),
        h('div', { class: 'track' }, h('span', { style: { width: (Math.min(1, c.share || 0) * 100).toFixed(2) + '%' } }))));
      return [
        h('h3', {}, t('byMonth'), isNum(sp.average) ? h('span', { class: 'h3-meta', text: t('avg', { x: m(sp.average) }) }) : null),
        chart(t('monthsTable'), h('div', {}, bars, axis), [t('date'), t('value')], months.map(x => [monthYear(x.month), m(x.total)])),
        (sp.top || []).length ? [h('h3', { text: t('topCats') }),
          chart(t('catsTable'), h('ul', { class: 'blist' }, cats), [t('item'), t('value'), t('share')],
            (sp.top || []).map(c => [pair(c.label), m(c.value), pct(c.share, false, 0)]))] : null];
    }

    // ---- Lo que tienes: one line with the total; the split, the largest holdings and the accounts open under it
    function barList(caption, rows) {
      const items = rows.map(r => h('li', {},
        h('div', { class: 'bl-line' }, h('span', { class: 'lbl' }, r.label, r.sub ? h('small', { text: r.sub }) : null),
          h('span', {}, h('span', { class: 'num', text: r.value }), isNum(r.weight) ? h('span', { class: 'share num', text: pct(r.weight) }) : null)),
        // A lone row at 100% has nothing to compare against: the figure says it all, no bar.
        isNum(r.weight) && rows.length > 1 ? h('div', { class: 'track' }, h('span', { style: { width: (Math.max(0, Math.min(1, r.weight)) * 100).toFixed(2) + '%' } })) : null));
      return chart(caption, h('ul', { class: 'blist' }, items), [t('item'), t('value'), t('share')],
        rows.map(r => [r.sub ? `${r.label} (${r.sub})` : r.label, r.value, pct(r.weight)]));
    }
    function renderHave(p) {
      const a = p.allocation || {};
      const accounts = p.accounts || [];
      if (a.status !== 'ready' && !accounts.length) return null;
      const out = [];
      if (a.status === 'ready') {
        out.push(h('div', { class: 'two' },
          h('div', {}, h('h3', { text: t('byClass') }), barList(t('byClass'), (a.asset_class || []).map(r => ({ label: has('ac_' + r.id) ? t('ac_' + r.id) : r.id, value: m(r.value), weight: r.weight })))),
          h('div', {}, h('h3', { text: t('byCurrency') }), barList(t('byCurrency'), (a.currency || []).map(r => ({ label: r.id, sub: isNum(r.native) ? money(r.native, r.id) : null, value: m(r.value), weight: r.weight }))))));
        if ((a.top || []).length) {
          out.push(h('h3', { text: t('topHold') }),
            barList(t('topHold'), a.top.map(r => ({ label: r.name, sub: r.symbols.length > 1 || r.symbols[0] !== r.name ? r.symbols.join(' + ') : null, value: m(r.value), weight: r.weight }))));
        }
        for (const o of a.overlaps || []) {
          const times = (t('overlapTimes') || {})[o.symbols.length] || `${o.symbols.length}×`;
          out.push(h('div', { class: 'callout' }, h('span', { class: 'badge', 'aria-hidden': 'true', text: `${o.symbols.length}×` }),
            h('p', {}, h('span', { class: 'v', text: `${o.name} ${times}` }), ': ', o.symbols.join(' + '),
              h('span', { class: 'quiet' }, ' · ', pct(o.weight), ' ', t('ofAssets')))));
        }
        if (a.venue || a.domicile) {
          // One segment at 100% is a sentence, not a bar.
          const split = (title, rows, prefix, tones) => rows && rows.length ? h('div', { class: 'split' },
            chart(title, h('div', {}, h('div', { class: 'quiet', text: title }),
              rows.length > 1 ? stack(rows.map((r, i) => ({ tone: tones[i % tones.length], value: r.value, title: `${t(prefix + r.id)}: ${pct(r.weight)}` }))) : null,
              h('ul', { class: 'legend' }, rows.map((r, i) => h('li', {}, rows.length > 1 ? swatch(tones[i % tones.length]) : null, t(prefix + r.id), h('span', { class: 'num', text: pct(r.weight, false, 0) }))))),
              [t('item'), t('share')], rows.map(r => [t(prefix + r.id), pct(r.weight)]))) : null;
          const venueTones = (a.venue || []).map((r, i) => r.id === 'unknown' ? 'open' : ['t1', 't2', 't3'][i % 3]);
          const domTones = (a.domicile || []).map((r, i) => r.id === 'unknown' ? 'open' : ['t1', 't3'][i % 2]);
          out.push(split(t('venue'), a.venue, 'v_', venueTones), split(t('domicile'), a.domicile, 'd_', domTones),
            h('p', { class: 'note', text: t('splitNote') }));
        }
      }
      if (accounts.length) {
        const rows = h('ul', { class: 'accts' }, accounts.map(r => {
          const when = r.as_of ? (r.source === 'said' ? t('saidOn', { d: railDate(r.as_of) }) : t('asOfShort', { d: railDate(r.as_of) })) : null;
          const why = r.unknown === 'balance' ? t('balUnknown') : r.unknown === 'fx' ? t('fxUnknown') : r.unknown ? t('pricesUnknown') : null;
          return h('li', {}, h('span', { class: 'name' }, String(r.name ?? '—'), h('small', { text: pair(r.kind) })),
            h('span', { class: 'val' }, h('span', { class: 'num', text: isNum(r.value) ? m(r.value) : '—' }), h('small', { text: why || when || '' })));
        }));
        const unknown = accounts.filter(r => !isNum(r.value)).length;
        const sub = h('details', { class: 'sub', open: openDiscs.has('accounts') || null },
          h('summary', {}, h('span', { text: t('accounts') }), h('span', { class: 'sum', text: unknown ? t('accountsSumUnknown', { n: num(accounts.length), u: num(unknown) }) : t('accountsSum', { n: num(accounts.length) }) })), rows);
        sub.addEventListener('toggle', () => { if (sub.open) openDiscs.add('accounts'); else openDiscs.delete('accounts'); });
        out.push(sub);
      }
      const biggest = a.status === 'ready' && (a.top || [])[0];
      const summary = [a.status === 'ready' ? h('span', {}, h('span', { class: 'num', text: m(a.total) }), ' ', t('inAssetsShort')) : null,
        biggest ? ` · ${biggest.name} ${pct(biggest.weight, false, 0)}` : null,
        accounts.length ? ` · ${t('accountsSum', { n: num(accounts.length) })}` : null].filter(Boolean);
      return disc('have', t('have'), summary, out);
    }

    // ---- Rendimiento: one line with the return against its reference; the chart opens under it
    function renderReturns(p) {
      const r = p.returns || {};
      const legacy = data.performance || {};
      const link = h('a', { class: 'rail-link', href: '/review' }, t('review'), h('span', { 'aria-hidden': 'true', text: '→' }));
      if (r.status !== 'ready') {
        if (legacy.status === 'ready') {
          return disc('returns', t('returns'), `${monthYear(legacy.start)} – ${monthYear(legacy.end)}`,
            figs([[t('twr'), null, pct(legacy.twr, true)], [t('mwr'), null, pct(legacy.irr_period, true)],
              legacy.benchmark ? [String(legacy.benchmark.name), null, pct(legacy.benchmark.return, true)] : null]), link);
        }
        return null;  // what is not known is named once, in the "not yet known" list
      }
      const keys = ['3m', '1y', 'all'].filter(k => r.periods && r.periods[k]);
      if (!keys.includes(period)) period = keys.includes('1y') ? '1y' : keys[keys.length - 1];
      const pp = r.periods[period];
      const pts = (r.series || []).filter(x => x.d >= pp.start);
      const selector = h('div', { class: 'periods', role: 'group', 'aria-label': t('periods') }, keys.map(k => h('button', {
        type: 'button', 'aria-pressed': String(k === period), text: t('p_' + k),
        onclick: () => { period = k; openDiscs.add('returns'); render(); document.querySelector(`#returns .periods [aria-pressed="true"]`)?.focus(); } })));
      const benchName = r.benchmark ? pair(r.benchmark) : null;
      const summary = h('span', {}, h('span', { class: 'num', text: pct(pp.twr, true) }), ' ', t('sincePeriod', { d: railDate(pp.start) }),
        isNum(pp.bench) ? [' · ', t('bench').toLowerCase(), ' ', h('span', { class: 'num', text: pct(pp.bench, true) })] : null);
      return disc('returns', t('returns'), summary,
        h('div', { class: 'sec-h' }, h('span'), selector),
        lineChart(pts, benchName),
        figs([[t('twr'), null, pct(pp.twr, true)], [t('mwr'), null, pct(pp.mwr, true)],
          benchName ? [t('bench'), benchName, pct(pp.bench, true)] : null]),
        (r.left_out || []).length ? h('p', { class: 'note', text: t('returnsLeft', { x: list(r.left_out) }) }) : null,
        h('div', {}, link));
    }
    function figs(rows) {
      return h('div', { class: 'figs' }, rows.filter(Boolean).map(([k, sub, v]) => h('div', {},
        h('span', { class: 'k2' }, k, sub ? h('small', { text: sub }) : null), h('span', { class: 'fv', text: v ?? '—' }))));
    }
    function lineChart(pts, benchName) {
      if (pts.length < 2) return null;
      const base = pts[0];
      const P = pts.map(x => x.p / base.p - 1);
      const B = benchName && isNum(base.b) ? pts.map(x => isNum(x.b) ? x.b / base.b - 1 : null) : null;
      const all = [...P, ...(B || []).filter(isNum), 0];
      let lo = Math.min(...all), hi = Math.max(...all);
      const pad = (hi - lo) * 0.1 || 0.01; lo -= pad; hi += pad;
      const X = i => i / (pts.length - 1) * 1000, Y = v => 1000 - (v - lo) / (hi - lo) * 1000;
      const poly = (vals, stroke, width, dash) => s('polyline', { points: vals.map((v, i) => isNum(v) ? `${X(i).toFixed(1)},${Y(v).toFixed(1)}` : null).filter(Boolean).join(' '),
        fill: 'none', stroke, 'stroke-width': width, 'stroke-dasharray': dash || null, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' });
      const plot = h('div', { class: 'plot' });
      plot.append(s('svg', { viewBox: '0 0 1000 1000', preserveAspectRatio: 'none' },
        s('line', { x1: 0, x2: 1000, y1: Y(0), y2: Y(0), stroke: 'var(--hairline)', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' }),
        B ? poly(B, 'var(--tone-3)', 1.5, '4 3') : null,
        poly(P, 'var(--ink)', 2)));
      const endP = P[P.length - 1], endB = B ? B[B.length - 1] : null;
      // The start value (0%) sits at the line's left end, inside the plot; the reference is named at its own end.
      plot.append(h('span', { class: 'zero', style: { top: (Y(0) / 10).toFixed(2) + '%' }, text: '0%' }));
      plot.append(h('span', { class: 'end-dot', style: { left: '100%', top: (Y(endP) / 10).toFixed(2) + '%' } }));
      // End labels: value first, series second; nudged apart only when they would touch.
      let yp = Y(endP) / 10, yb = isNum(endB) ? Y(endB) / 10 : null;
      if (yb !== null && Math.abs(yp - yb) < 18) { const mid = (yp + yb) / 2; yp = mid + (yp >= yb ? 9 : -9); yb = mid + (yp >= yb ? -9 : 9); }
      const tags = [h('span', { class: 'tag', style: { top: yp.toFixed(2) + '%' } }, pct(endP, true), h('small', { text: t('you2') }))];
      if (yb !== null) tags.push(h('span', { class: 'tag', style: { top: yb.toFixed(2) + '%', color: 'var(--ink-2)' } }, pct(endB, true), h('small', { text: benchName })));
      plot.append(...tags);
      // Crosshair: snaps to the nearest week; the same readout on keyboard focus.
      const cross = h('div', { class: 'cross', hidden: true }), tip = h('div', { class: 'tip', hidden: true });
      const hit = h('div', { class: 'hit', tabindex: '0', role: 'img', 'aria-label': t('returnsTable') });
      let at = pts.length - 1;
      const show = i => {
        at = Math.max(0, Math.min(pts.length - 1, i));
        const left = (X(at) / 10).toFixed(2) + '%';
        cross.hidden = false; tip.hidden = false; cross.style.left = left; tip.style.left = left;
        fill(tip, h('span', { class: 'quiet', text: railDate(pts[at].d) }),
          h('b', {}, h('span', { class: 'k' }), pct(P[at], true)),
          B && isNum(B[at]) ? h('b', {}, h('span', { class: 'k b' }), pct(B[at], true)) : null);
      };
      const hide = () => { cross.hidden = true; tip.hidden = true; };
      hit.addEventListener('pointermove', e => { const box = hit.getBoundingClientRect(); show(Math.round((e.clientX - box.left) / box.width * (pts.length - 1))); });
      hit.addEventListener('pointerleave', hide);
      hit.addEventListener('focus', () => show(at));
      hit.addEventListener('blur', hide);
      hit.addEventListener('keydown', e => { if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); show(at + (e.key === 'ArrowRight' ? 1 : -1)); } });
      plot.append(cross, tip, hit);
      const axis = h('div', { class: 'axis' }, h('span', { text: railDate(pts[0].d) }), h('span', { text: railDate(pts[pts.length - 1].d) }));
      // Once drawn, the right margin is exactly what the widest end label needs.
      requestAnimationFrame(() => {
        if (!plot.isConnected) return;
        const width = `${Math.ceil(Math.max(...tags.map(x => x.getBoundingClientRect().width))) + 16}px`;
        plot.style.marginRight = width; axis.style.marginRight = width;
      });
      const head = [t('date'), t('you2'), ...(B ? [benchName] : [])];
      return h('figure', { class: 'chart', 'aria-label': t('returnsTable') }, plot,
        h('div', { 'aria-hidden': 'true' }, axis),
        srTable(t('returnsTable'), head, pts.map((x, i) => [railDate(x.d), pct(P[i], true), ...(B ? [pct(B[i], true)] : [])])));
    }

    // ---- Lo que debes: one line with the total and the month it ends; each debt opens as what is left to pay
    function renderOwe(p) {
      const debts = p.debts || [];
      if (!debts.length) return null;  // "do you owe anything?" is asked once, in the "not yet known" list
      const total = debts.reduce((a, d) => a + (isNum(d.value) ? d.value : 0), 0);
      const last = debts.filter(d => d.payoff).sort((a, b) => String(b.payoff).localeCompare(String(a.payoff)))[0];
      const summary = h('span', {}, h('span', { class: 'num', text: m(total) }), last ? ` · ${t('paidBy', { d: monthYear(last.payoff) })}` : '');
      return disc('owe', t('owe'), summary, debts.map(d => {
        const name = String(d.name ?? pair(d.kind));
        const facts = [];
        const fact = (value, label) => h('span', {}, h('span', { class: 'num', text: value }), ' ', label);
        if (isNum(d.rate)) facts.push(fact(pct(d.rate, false, 2), t('rateK')));
        if (isNum(d.payment)) facts.push(fact(m(d.payment, d.currency), t('perMonthK')));
        const missing = (d.missing || []).map(k => t(k === 'rate' ? 'missRate' : 'missPayment'));
        return h('div', { class: 'debt' },
          h('div', { class: 'debt-head' }, h('span', { class: 'name' }, name, h('small', { text: [d.lender, pair(d.kind) !== name ? pair(d.kind) : null].filter(Boolean).join(' · ') })),
            h('span', { class: 'bal num', text: isNum(d.balance) ? m(d.balance, d.currency) : '—' })),
          d.payoff && isNum(d.balance) ? payoffBar(d) : null,
          facts.length ? h('div', { class: 'debt-facts' }, facts) : null,
          d.status === 'never' ? h('p', { class: 'note', text: t('never') }) : null,
          missing.length ? h('p', { class: 'note' }, '— ', t('missing', { x: list(missing) }), ' ', tellBtn(t('askDebt', { x: name }))) : null);
      }));
    }
    // What is left to pay, as one bar: the balance in ink, the interest still to come hatched, the end month at the right.
    function payoffBar(d) {
      const interest = isNum(d.interest) ? d.interest : 0;
      const parts = [{ tone: 't1', value: d.balance, title: `${t('balance')}: ${m(d.balance, d.currency)}` }];
      if (interest > 0) parts.push({ tone: 'hatch', value: interest, title: `${t('interestK')}: ${m(interest, d.currency)}` });
      const visual = h('div', { class: 'payoff' }, stack(parts),
        h('div', { class: 'payoff-x' },
          h('span', {}, interest > 0 ? [m(d.balance, d.currency), ' + ', h('span', { class: 'num', text: m(interest, d.currency) }), ' ', t('interestK')] : m(d.balance, d.currency)),
          h('span', { class: 'v', text: monthYear(d.payoff) })));
      return chart(`${t('payoffTable')}: ${d.name || pair(d.kind)}`, visual, [t('item'), t('value')],
        [[t('balance'), m(d.balance, d.currency)], [t('interestK'), m(interest, d.currency)], [t('date'), monthYear(d.payoff)]]);
    }

    // ---- Metas: the tile shows each goal as one bar, and a plan as its twelve dots
    function dotsFor(adh) {
      // Twelve months always: the plan's judged installments, then pending ones; the next due is the live one.
      const inst = (adh.installments || []).slice(-12).map(i => ({ ...i }));
      let last = inst.length ? asDate(inst[inst.length - 1].due) : asDate(data?.today);
      while (inst.length < 12 && last) {
        last = new Date(Date.UTC(last.getUTCFullYear(), last.getUTCMonth() + 1, Math.min(28, last.getUTCDate())));
        inst.push({ due: last.toISOString().slice(0, 10), state: 'pending' });
      }
      const next = inst.find(i => i.state === 'pending');
      if (next) next.next = true;
      return inst;
    }
    function renderGoalsTile(p) {
      const goals = p.goals || [];
      if (!goals.length) return null;
      return h('section', { class: 'tile', 'aria-labelledby': 'goals-h' }, h('h2', { id: 'goals-h', text: t('goals') }), goals.slice(0, 3).map(g => {
        const c = g.currency;
        const known = ['on_track', 'behind', 'funded'].includes(g.status);
        const head = h('div', { class: 'goal-head' }, h('span', { class: 'name', text: String(g.name ?? '') }),
          known ? h('span', { class: 'status ' + g.status, text: t('s_' + g.status) }) : null);
        const body = [];
        if (isNum(g.target)) {
          // Progress reads unknown when nothing is tied to the goal yet: an empty bar, a dash, never $0.
          const ratio = isNum(g.funded) ? Math.max(0, Math.min(1, g.ratio || 0)) : 0;
          const proj = isNum(g.projected) ? Math.max(0, Math.min(1, g.projected / g.target)) : null;
          const bar = h('div', { class: 'progress' }, isNum(g.funded) ? h('span', { class: 'fill', style: { width: (ratio * 100).toFixed(2) + '%' } }) : null,
            proj !== null && proj > ratio ? h('span', { class: 'proj', title: t('projected', { x: m(g.projected, c) }), style: { left: (proj * 100).toFixed(2) + '%' } }) : null);
          const x = h('div', { class: 'progress-x' },
            h('span', {}, h('span', { class: 'v', text: isNum(g.funded) ? m(g.funded, c) : '—' }), ' / ', m(g.target, c)),
            g.due ? h('span', { text: t('by', { d: monthYear(g.due) }) }) : null);
          body.push(chart(`${t('goalTable')}: ${g.name}`, h('div', {}, bar, x), [t('item'), t('value')],
            [[t('amount'), m(g.target, c)], [t('goalTable'), isNum(g.funded) ? m(g.funded, c) : '—'], ...(isNum(g.projected) ? [[t('atPace'), m(g.projected, c)]] : []),
              ...(g.due ? [[t('date'), monthYear(g.due)]] : [])]));
          if (g.status === 'behind' && isNum(g.needed)) body.push(h('p', { class: 'note', text: t('needs', { n: m(g.needed, c), m: isNum(g.monthly) ? m(g.monthly, c) : '—' }) }));
          else if (!isNum(g.funded)) body.push(h('p', { class: 'note', text: t('fundedUnknown') }));
          else if (!isNum(g.monthly) && g.status !== 'funded') body.push(h('p', { class: 'note', text: t('noPlanMonthly') }));
        } else if (isNum(g.monthly)) {
          body.push(h('p', { class: 'note', text: t('monthlyGoal', { x: m(g.monthly, c) }) }));
        }
        const adh = g.plan?.adherence;
        if (adh && (adh.installments || []).length) {
          const inst = dotsFor(adh);
          const judged = inst.filter(i => !['pending', 'unknown'].includes(i.state));
          const done = judged.filter(i => i.state === 'on_time').length;
          const dots = h('div', { class: 'dots' }, inst.map(i => h('i', { class: i.state + (i.next ? ' next' : ''), title: `${railDate(i.due)}: ${t(i.next ? 'st_next' : 'st_' + i.state)}` })));
          body.push(chart(`${t('dotsTable')}: ${g.plan.name}`, dots, [t('date'), t('status')], inst.map(i => [railDate(i.due), t(i.next ? 'st_next' : 'st_' + i.state)])),
            h('p', { class: 'note' }, t('onTime', { a: num(done), b: num(judged.length) }),
              isNum(adh.invested) && isNum(adh.planned) ? [' · ', t('invested', { a: m(adh.invested, adh.currency), b: m(adh.planned, adh.currency) })] : ''));
        }
        return h('div', { class: 'goal' }, head, body);
      }));
    }

    // ---- Reserva: months covered against the target
    function renderReserveTile(p) {
      const r = p.reserve || {};
      if (!isNum(r.months)) return null;  // without essential spending there are no months to show; asked once above
      const target = isNum(r.target_months) ? r.target_months : null;
      const max = Math.max(target || 0, r.months) * 1.15 || 1;
      const gauge = h('div', { class: 'gauge' }, h('span', { class: 'rail' }),
        h('span', { class: 'fill', style: { width: (r.months / max * 100).toFixed(2) + '%' } }),
        target ? h('span', { class: 'tick', style: { left: (target / max * 100).toFixed(2) + '%' } }) : null);
      const axis = h('div', { class: 'gauge-x' }, h('span', { class: 'start', style: { left: 0 }, text: '0' }),
        target ? h('span', { style: { left: (target / max * 100).toFixed(2) + '%' }, text: t('gaugeTarget', { t: num(target, 1) }) }) : null);
      const figure = h('div', { class: 'rate' }, h('span', { class: 'figure-s num', text: num(r.months, 1) }),
        h('span', { class: 'quiet', text: target ? t('ofMonths', { t: num(target, 1) }) : t('monthsK') }));
      const rows = [[t('reserve'), `${num(r.months, 1)} ${t('monthsK')}`], ...(target ? [[t('target'), `${num(target, 1)} ${t('monthsK')}`]] : [])];
      return h('section', { class: 'tile', 'aria-labelledby': 'reserve-h' }, h('h2', { id: 'reserve-h', text: t('reserve') }), figure,
        chart(t('reserveTable'), h('div', {}, gauge, axis), [t('item'), t('value')], rows),
        isNum(r.target_amount) ? h('p', { class: 'note', text: isNum(r.gap) && r.gap > 0
          ? t('gapLeft', { a: m(r.amount), b: m(r.target_amount), g: m(r.gap) }) : `${m(r.amount)} · ${t('reserveMet')}` }) : null);
    }

    // ---- Lo que sé de ti: one sentence per group; the facts behind each open on tap, and a sentence is edited by tapping it
    // Herencia / Estate: one collapsed line with the completeness score; the top gap opens under it.
    function renderEstate() {
      const e = data.estate;
      if (!e || !isNum(e.score)) return null;
      const line = (e.top_gap && e.top_gap[lang]) || (e.question && e.question[lang]) || t('estateClear');
      return disc('estate', t('estate'), t('estateScore', { n: num(e.score) }), h('p', { class: 'note', text: line }));
    }
    function renderMemory(mem) {
      const groups = mem.groups.map(g => ({ ...g, facts: g.facts.filter(f => !hidden.has(f.text)) })).filter(g => g.facts.length);
      const facts = groups.reduce((a, g) => a + g.facts.length, 0) + (mem.review || []).length;
      const due = passQueue(mem).length;
      const open = memoryOpen || editing !== null;
      const toggle = h('button', { class: 'memory-toggle', type: 'button', 'aria-expanded': String(open), 'aria-controls': 'memory-body',
        onclick: () => { memoryOpen = !open; editing = null; render(); document.querySelector('.memory-toggle')?.focus(); } },
        h('span', { class: 'h', text: t('knows') }),
        h('span', { class: 'm' }, h('span', { text: t('factsN', { n: num(facts) }) }), h('span', { class: 'chev', 'aria-hidden': 'true', text: '⌄' })));
      const dueLine = due && writes ? h('p', { class: 'due-line' }, h('span', { class: 'mark', 'aria-hidden': 'true' }),
        t(due === 1 ? 'dueOne' : 'dueN', { n: num(due) }), ' ',
        h('button', { class: 'txt act', type: 'button', 'aria-haspopup': 'dialog', text: t('reviewNow'), onclick: openPass })) : null;
      const area = g => {
        const id = 'g-' + g.id;
        // The group's one sentence: its summary, or (without one) its first fact, which then is not repeated below.
        const lead = g.summary || g.facts[0];
        const rest = g.summary ? g.facts : g.facts.slice(1);
        const box = h('details', { class: 'area', open: openDiscs.has(id) || (editing !== null && g.facts.some(f => f.id === editing)) || null },
          h('summary', {}, h('h3', { id, text: t('g_' + g.id) }),
            h('p', { class: 'summary' }, sentence(lead.text, lead.emphasis)),
            h('span', { class: 'count', text: t('factsN', { n: num(g.facts.length) }) })),
          rest.length ? h('ul', { class: 'facts' }, rest.map(renderFact)) : h('ul', { class: 'facts' }, renderFact(g.facts[0])));
        box.addEventListener('toggle', () => { if (box.open) openDiscs.add(id); else openDiscs.delete(id); });
        return box;
      };
      return h('section', { class: 'memory', 'aria-label': t('knows') }, toggle, dueLine,
        open ? h('div', { class: 'memory-body', id: 'memory-body' }, groups.map(area)) : h('div', { id: 'memory-body', hidden: true }));
    }
    function renderFact(item) {
      const guess = item.origin?.kind === 'guess';
      const canEdit = item.edit && writes;
      const words = sentence(item.text, item.emphasis);
      const text = canEdit || item.key
        ? h('button', { class: 'fact-say', type: 'button', 'data-say': item.id,
            'aria-haspopup': canEdit ? null : 'dialog', 'aria-expanded': canEdit ? String(editing === item.id) : null,
            onclick: () => { if (canEdit) { editing = editing === item.id ? null : item.id; render(); } else openSheet(item); } }, words)
        : h('p', { class: 'fact-say plain', 'data-say': item.id }, words);
      return h('li', { class: 'fact' + (guess ? ' guess' : '') }, text, editing === item.id && canEdit ? editor(item, item.edit) : null);
    }

    // ------------------------------------------------------------ Hoy, connections, your data
    async function loadSurfaces() {
      const get = path => fetch(path, { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Wealth-Token': token } })
        .then(r => r.ok ? r.json() : null).catch(() => null);
      let zone = '';
      try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (_) { /* optional */ }
      [todayData, conns] = await Promise.all([get('/api/today?tz=' + encodeURIComponent(zone)), get('/api/connections')]);
    }
    // Other pages hand the chat a prompt through sessionStorage, never the address.
    function handoff(text, send) {
      try { sessionStorage.setItem('wealth.handoff', JSON.stringify({ text, send: !!send })); } catch (_) { /* the chat still opens */ }
      location.href = '/';
    }
    function sinceText(iso) {
      const when = new Date(iso);
      if (Number.isNaN(when.getTime())) return '';
      const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
      const hours = (Date.now() - when.getTime()) / 36e5;
      if (hours < 1) return rtf.format(-Math.max(1, Math.round(hours * 60)), 'minute');
      if (hours < 24) return rtf.format(-Math.round(hours), 'hour');
      return rtf.format(-Math.round(hours / 24), 'day');
    }
    function accountRows(all) {
      const accounts = all.filter(a => a.stale);
      if (!accounts.length) return null;
      return h('ul', { class: 'accts' }, accounts.map(a => h('li', {},
        h('span', { class: 'name', text: a.label }),
        h('span', { class: 'acct-fresh' + (a.stale ? ' stale' : '') }, a.as_of ? t('asOfShort', { d: railDate(a.as_of) }) : t('unknownS')))));
    }
    function renderConnections() {
      if (!conns) return null;
      const rows = (conns.connectors || []).map(c => {
        const state = c.configured ? t(c.via === 'env' ? 'keyEnv' : 'keyKeychain') : t('noKey');
        const ask = t('syncAsk', { i: c.institution }) + (c.name === 'ibkr_flex' ? t('syncAskIbkr') : '');
        return h('li', { class: 'conn' },
          h('div', { class: 'conn-head' }, h('span', { class: 'conn-name', text: c.institution }),
            h('span', { class: 'conn-state' + (c.configured ? ' on' : ''), text: state })),
          c.last_sync ? h('p', { class: 'conn-meta', text: t('synced', { t: sinceText(c.last_sync) }) }) : null,
          accountRows(c.accounts || []),
          h('div', { class: 'acts' },
            c.configured ? h('button', { class: 'txt', type: 'button', text: t('sync'), onclick: () => handoff(ask, false) }) : null,
            h('button', { class: 'txt', type: 'button', 'aria-haspopup': 'dialog', text: t('howTo'), onclick: () => openSetup(c) })));
      });
      rows.push(h('li', { class: 'conn' },
        h('div', { class: 'conn-head' }, h('span', { class: 'conn-name', text: t('statements') })),
        accountRows(conns.statements?.accounts || []),
        h('div', { class: 'acts' }, h('button', { class: 'txt', type: 'button', text: t('uploadRow'), onclick: () => handoff(t('uploadAsk'), false) }))));
      // One line says the state; the connectors, the export and the erase line open under it.
      const on = (conns.connectors || []).filter(c => c.configured).length, off = (conns.connectors || []).length - on;
      const summary = on ? t('connsSum', { n: num(on), m: num(off) }) : t('connsNone');
      const stale = [...(conns.connectors || []).flatMap(c => c.accounts || []), ...(conns.statements?.accounts || [])].filter(a => a.stale).length;
      return disc('conns', t('conns'), stale ? `${summary} · ${t('staleN', { n: num(stale) })}` : summary,
        h('ul', { class: 'conn-list' }, rows), renderData());
    }
    function openSetup(c) {
      const setup = c.setup || {};
      const commands = (setup.commands || []).join('\n');
      const copy = h('button', { class: 'txt small', type: 'button', text: t('copy'), onclick: async () => {
        try { await navigator.clipboard.writeText(commands); say(t('copied')); } catch (_) { /* the text stays selectable */ }
      } });
      const step = (no, ...kids) => h('li', {}, h('span', { class: 'step-n', 'aria-hidden': 'true', text: String(no) }), h('div', {}, kids));
      fill(sheet,
        h('button', { class: 'txt close', type: 'button', text: t('close'), onclick: () => sheet.close() }),
        h('p', { class: 'say', id: 'sheet-title', text: t('sheetTitle', { i: c.institution }) }),
        h('ol', { class: 'steps' },
          step(1, t('step1_' + c.name)),
          step(2, t('step2'), h('pre', { class: 'cmd' }, h('code', { text: commands })), copy),
          step(3, t('step3'), h('p', { class: 'env', text: (setup.env || []).join('  ') })),
          step(4, has('step4_' + c.name) ? t('step4_' + c.name) : t('step4'))),
        h('p', { class: 'quiet', text: t('readOnlyLine') }));
      sheet.dataset.from = '';
      if (!sheet.open) sheet.showModal();
    }
    async function downloadExport(button) {
      button.disabled = true;
      try {
        const response = await fetch('/api/export', { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Wealth-Token': token } });
        if (!response.ok) throw new Error(t('exportFailed'));
        const url = URL.createObjectURL(await response.blob());
        const link = h('a', { href: url, download: 'wealth-export.json' });
        document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 4000);
      } catch (_) { say(t('exportFailed')); } finally { button.disabled = false; }
    }
    // The annual tax pack for the last completed year: a printable page (print to PDF) from /api/tax-pack.
    async function downloadTaxPack(button, year) {
      button.disabled = true;
      try {
        const response = await fetch(`/api/tax-pack?year=${year}&format=html&lang=${lang}`,
          { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Wealth-Token': token } });
        if (!response.ok) throw new Error(t('taxPackFailed'));
        const url = URL.createObjectURL(await response.blob());
        const link = h('a', { href: url, download: `tax-pack-${year}.html` });
        document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 4000);
      } catch (_) { say(t('taxPackFailed')); } finally { button.disabled = false; }
    }
    // Two quiet lines. Deleting happens only in the Terminal: the command stays folded until the line is tapped.
    function renderData() {
      if (!conns?.data) return null;
      const taxYear = new Date().getFullYear() - 1;
      const command = String(conns.data.forget_command || '');
      const copy = h('button', { class: 'txt small', type: 'button', text: t('copy'), onclick: async () => {
        try { await navigator.clipboard.writeText(command); say(t('copied')); } catch (_) { /* the text stays selectable */ }
      } });
      const how = h('div', { class: 'erase-how', id: 'erase-how', hidden: true },
        h('p', { class: 'quiet', text: t('eraseLine') }),
        h('pre', { class: 'cmd' }, h('code', { text: command })), copy);
      const erase = h('button', { class: 'txt', type: 'button', text: t('erase'), 'aria-expanded': 'false', 'aria-controls': 'erase-how',
        onclick: e => { how.hidden = !how.hidden; e.currentTarget.setAttribute('aria-expanded', String(!how.hidden)); } });
      return h('div', { class: 'data' },
        h('div', { class: 'data-row' }, h('button', { class: 'txt', type: 'button', text: t('taxPack', { y: taxYear }), onclick: e => downloadTaxPack(e.currentTarget, taxYear) })),
        h('div', { class: 'data-row' }, h('button', { class: 'txt', type: 'button', text: t('export'), onclick: e => downloadExport(e.currentTarget) })),
        h('div', { class: 'data-row' }, erase),
        how);
    }

    // ------------------------------------------------------------ boot
    // The language comes before the first paint: a saved choice on this device, else the one the person
    // set up Wealth in (/api/state), else the browser's; the page never renders in one language and flips.
    function setLang(next, persist) {
      lang = next === 'es' ? 'es' : 'en';
      const nav = navigator.language || 'en-US';
      locale = lang === 'es' ? 'es-MX' : (nav.toLowerCase().startsWith('en') ? nav : 'en-US');
      document.documentElement.lang = lang === 'es' ? 'es-MX' : 'en';
      document.title = `${t('you')} · Wealth`;
      if (persist) { try { localStorage.setItem('wealth.lang', lang); } catch (_) { /* optional */ } }
      document.querySelectorAll('#lang button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.lang === lang)));
      document.getElementById('back').textContent = t('chat');
      editing = null;
      if (data && data.memory && data.memory[lang]) render();
      else if (data) load();  // sentences come from the server in the page's language
    }
    document.querySelectorAll('#lang button').forEach(b => b.addEventListener('click', () => setLang(b.dataset.lang, true)));
    (async () => {
      let stored = null, asked = null;
      try { stored = localStorage.getItem('wealth.lang'); } catch (_) { /* optional */ }
      try { asked = new URLSearchParams(location.search).get('lang'); } catch (_) { /* optional */ }
      const state = await fetch('/api/state', { credentials: 'same-origin', cache: 'no-store' }).then(r => r.ok ? r.json() : {}).catch(() => ({}));
      token = String(state.csrf_token || '');
      const saved = ['es', 'en'].includes(state.language) ? state.language : null;
      // The address wins (?lang=en), then this device's choice, then the language Wealth was set up in, then the browser's.
      setLang(['es', 'en'].includes(asked) ? asked : stored || saved || ((navigator.language || '').toLowerCase().startsWith('es') ? 'es' : 'en'), false);
      await load();
    })();
  })();
