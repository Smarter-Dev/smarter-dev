// Runs chat.js's send path against a stubbed fetch under node.
//
// A page left open while its model is retired or disabled finds out when the
// server refuses a send with the model_unavailable 409. That refusal alone
// must lock the composer, show the notice and hand the draft back; any other
// 409 keeps today's behaviour.

function check(condition, description) {
  if (!condition) {
    console.error('FAIL: ' + description);
    process.exitCode = 1;
  }
}

function element(extra) {
  // The splice runs the listeners registered after sendMessage too.
  var el = {disabled: false, hidden: false, textContent: '', value: '', dataset: {},
    addEventListener: function () {}};
  Object.keys(extra || {}).forEach(function (key) { el[key] = extra[key]; });
  return el;
}

var keyName = element();
var unavailableNotice = element({
  hidden: true,
  querySelector: function (selector) {
    return selector === '[data-model-unavailable-key]' ? keyName : null;
  },
});
var shell = element({dataset: {modelKey: 'gpt-6-1-sol', reasoningLevel: 'high', modelAvailable: 'true'}});
var input = element();
var submitBtn = element();
var form = element();
var hintEl = element();
var stopBtn = element();
var errorEl = element({hidden: true});
var statusEl = element();

var thread = {
  children: [],
  querySelector: function () { return null; },
  insertBefore: function (node) { node.parent = thread; thread.children.push(node); },
};
function bubble(role, text) {
  var content = {textContent: text};
  return {
    role: role,
    dataset: {},
    querySelector: function () { return content; },
    content: content,
    remove: function () {
      thread.children = thread.children.filter(function (node) { return node !== this; }, this);
    },
  };
}

// Just enough of a <select> for the controls to be rebuilt for real.
function select() {
  var el = element();
  el.options = [];
  el.listeners = 0;
  el.addEventListener = function (type, handler) { el.listeners += 1; el.handler = handler; };
  el.appendChild = function (option) { el.options.push(option); };
  Object.defineProperty(el, 'textContent', {
    get: function () { return ''; },
    set: function () { el.options = []; },
  });
  Object.defineProperty(el, 'value', {
    get: function () { return el.current || ''; },
    set: function (value) {
      el.current = el.options.some(function (o) { return o.value === value; }) ? value : '';
    },
  });
  el.querySelectorAll = function () { return []; };
  return el;
}
var modelSelect = select();
var reasoningSelect = select();
var settingsDisclosure = element({hidden: true});
var regenerate = element({dataset: {regenerate: '', turnId: 't-1'}});
var controls = {
  '[data-chat-model]': modelSelect,
  '[data-chat-reasoning]': reasoningSelect,
  '[data-regenerate]': regenerate,
};

// The listeners chat.js registers on the document, by type, so the real
// regenerate click handler can be driven.
var documentListeners = {};
var document = {
  addEventListener: function (type, handler) {
    (documentListeners[type] = documentListeners[type] || []).push(handler);
  },
  createElement: function () { return {value: '', textContent: '', dataset: {}}; },
  querySelectorAll: function (selector) { return controls[selector] ? [controls[selector]] : []; },
  querySelector: function (selector) { return controls[selector] || null; },
};

var IDLE_HINT = 'shift+enter · newline';
var UNAVAILABLE_HINT = 'Choose an available model to continue';
var mode = 'chat';
var uploadCount = 0;
var activeTurn = null;
var conversationId = 'c-1';
var csrfToken = 't';
var modelUnavailable = false;
var catalog = null;
var pendingChange = null;
var pendingAttachments = [];
var autoGrown = 0;

function json(method, body) { return {method: method, body: JSON.stringify(body)}; }
function setStatus() {}
function scrollToBottom() {}
function autoGrow() { autoGrown += 1; }
function submitResources() { throw new Error('not in chat mode'); }
function submissionKey() { return 'k'; }
function createConversation() { throw new Error('the conversation exists'); }
function proposeModel() {}
function syncModelLabel() {}

var requests = [];
var replies = [];
function reply(status, body) { replies.push({status: status, body: body}); }
function fetch(url, options) {
  requests.push([options && options.method || 'GET', url]);
  // An unscripted request fails rather than crashing, so the checks name it.
  var next = replies.shift() || {status: 599, body: {detail: 'unexpected request'}};
  return Promise.resolve({
    ok: next.status < 400,
    status: next.status,
    json: function () { return Promise.resolve(next.body); },
  });
}

// ── Functions under test, spliced from chat.js ────────────
// <CHAT_JS_FUNCTIONS>

function settle() { return new Promise(function (resolve) { setTimeout(resolve, 0); }); }

function submit(text) {
  input.value = text;
  // The form handler clears the box once sendMessage accepts the draft.
  if (sendMessage(input.value)) input.value = '';
}

// A click on the Regenerate button, through every document click listener.
function clickRegenerate() {
  var event = {target: {closest: function (selector) {
    return selector === '[data-regenerate]' ? regenerate : null;
  }}};
  documentListeners.click.forEach(function (handler) { handler(event); });
}

// A new reasoning level chosen, through the listener bound on the select.
function chooseReasoning(level) {
  reasoningSelect.value = level;
  reasoningSelect.handler({target: reasoningSelect});
}

function keys(el) { return el.options.map(function (o) { return o.value; }); }

function reset() {
  thread.children = [];
  requests = [];
  pendingChange = null;
  autoGrown = 0;
  input.value = '';
  catalog = CATALOG_WITH;
  activateConversationControls('gpt-6-1-sol', 'high');
  showError('');
}

var UNAVAILABLE = {status_code: 409, detail: 'The selected model is unavailable; choose a new model.', code: 'model_unavailable'};
function model(key, levels) {
  return {key: key, label: key, cost_tier: '$', reasoning_levels: levels};
}
var LUNA = model('gpt-6-luna', ['low', 'high']);
var SOL = model('gpt-6-1-sol', ['low', 'medium', 'high']);
var GROK = model('grok-4-5', ['high']);
var CATALOG_WITHOUT = {models: [LUNA, GROK]};
var CATALOG_WITH = {models: [LUNA, SOL]};

async function main() {
  // The page loaded while the selection was available.
  catalog = CATALOG_WITH;
  activateConversationControls('gpt-6-1-sol', 'high');
  check(!modelUnavailable && keys(modelSelect).join() === 'gpt-6-luna,gpt-6-1-sol', 'the page starts available');

  // A model disabled after the page loaded.
  reset();
  reply(409, UNAVAILABLE);
  reply(200, CATALOG_WITHOUT);
  submit('Keep this draft');
  await settle();
  check(modelUnavailable, 'the refusal marks the model unavailable');
  check(!unavailableNotice.hidden, 'the notice shows without a reload');
  check(keyName.textContent === 'gpt-6-1-sol', 'the notice names the stored key');
  check(input.disabled && submitBtn.disabled, 'the composer locks');
  check(hintEl.textContent === UNAVAILABLE_HINT, 'the hint says why');
  check(input.value === 'Keep this draft', 'the draft is handed back');
  check(autoGrown === 1, 'the box regrows around the returned draft');
  check(thread.children.length === 0, 'the refused exchange leaves the thread');
  check(!errorEl.hidden && errorEl.textContent === UNAVAILABLE.detail, 'the refusal is said');
  check(requests.length === 2 && requests[1][1] === '/v2/api/chat/catalog', 'the catalog is reloaded');
  check(keys(modelSelect).join() === 'gpt-6-luna,grok-4-5,gpt-6-1-sol',
    'the select now offers the catalog, with the stored key last');
  check(modelSelect.options[2].textContent === 'gpt-6-1-sol · unavailable (select a new model)'
    && 'unavailable' in modelSelect.options[2].dataset, 'the stored key is marked unavailable, as after a reload');
  check(modelSelect.value === 'gpt-6-1-sol', 'the select still shows the stored key');
  check(keys(reasoningSelect).join() === '', 'reasoning falls back to the model default only');
  check(reasoningSelect.disabled, 'reasoning is locked');
  check(!modelSelect.disabled, 'the model select stays usable to recover');
  check(!settingsDisclosure.hidden, 'the settings stay reachable');
  check(sendMessage('again') === false, 'a second send is stopped before the network');
  check(requests.length === 2, 'and makes no request');

  // Something typed while the request was out is not overwritten.
  reset();
  reply(409, UNAVAILABLE);
  reply(200, CATALOG_WITHOUT);
  submit('First draft');
  input.value = 'Typed since';
  await settle();
  check(input.value === 'Typed since', 'a newer draft is not overwritten');
  check(thread.children.length === 2 && thread.children[0].content.textContent === 'First draft',
    'the refused words stay on the thread instead');
  check(thread.children.length === 2 && thread.children[1].content.textContent === UNAVAILABLE.detail,
    'beside the refusal');
  check(modelUnavailable, 'the composer still locks');

  // A question sent from the quote box is not a composer draft.
  reset();
  reply(409, UNAVAILABLE);
  reply(200, CATALOG_WITHOUT);
  check(sendMessage('> passage\n\nquestion', function () {}), 'the quoted send goes out');
  await settle();
  check(input.value === '', 'a quoted question is not poured into the composer');
  check(thread.children.length === 2 && thread.children[0].content.textContent === '> passage\n\nquestion',
    'it stays on the thread');
  check(modelUnavailable, 'the composer locks');

  // A change awaiting confirmation keeps its target showing.
  reset();
  modelSelect.value = 'gpt-6-luna';
  pendingChange = {id: 'change-1'};
  reply(409, UNAVAILABLE);
  reply(200, CATALOG_WITHOUT);
  submit('Mid-change');
  await settle();
  check(modelSelect.value === 'gpt-6-luna', 'the proposed model still shows under the open dialog');
  check(modelSelect.dataset.original === 'gpt-6-1-sol', 'cancel still returns to the stored key');
  check(modelSelect.listeners === 1 && reasoningSelect.listeners === 1, 'rebuilding binds no second listener');

  // Re-enabled between the refusal and the catalog read: the catalog decides.
  reset();
  reply(409, UNAVAILABLE);
  reply(200, CATALOG_WITH);
  submit('Try again');
  await settle();
  check(!modelUnavailable && unavailableNotice.hidden, 'a catalog that lists the model unlocks again');
  check(input.value === 'Try again', 'the draft is still there to resend');

  // The catalog cannot be read: stay locked rather than guess.
  reset();
  reply(409, UNAVAILABLE);
  reply(500, {detail: 'Internal Server Error'});
  submit('Offline');
  await settle();
  check(modelUnavailable && !unavailableNotice.hidden, 'the lock holds when the catalog fails');

  // Any other conflict is not a retirement.
  reset();
  reply(409, {status_code: 409, detail: 'Wait for the active turn to finish or stop it.'});
  submit('Busy');
  await settle();
  check(!modelUnavailable && unavailableNotice.hidden, 'an unrelated 409 shows no notice');
  check(!input.disabled && !submitBtn.disabled, 'an unrelated 409 does not lock the composer');
  check(requests.length === 1, 'an unrelated 409 does not reload the catalog');
  check(thread.children.length === 2, 'an unrelated 409 keeps its exchange on the thread');
  check(thread.children.length === 2 && thread.children[1].content.textContent === 'Wait for the active turn to finish or stop it.',
    'and shows its error in place, as before');

  // Regenerate, through its real click listener.
  reset();
  reply(409, UNAVAILABLE);
  reply(200, CATALOG_WITHOUT);
  clickRegenerate();
  await settle();
  check(requests[0][0] === 'POST' && requests[0][1] === '/v2/api/chat/conversations/c-1/turns/t-1/regenerate',
    'the click asks to regenerate');
  check(modelUnavailable && !unavailableNotice.hidden, 'a refused regenerate shows the notice');
  check(input.disabled && submitBtn.disabled && regenerate.disabled, 'and locks the composer and Regenerate');
  check(requests.length === 2 && requests[1][1] === '/v2/api/chat/catalog', 'and reloads the catalog');
  requests = [];
  clickRegenerate();
  check(requests.length === 0, 'a locked page sends no second regenerate');

  reset();
  reply(409, {status_code: 409, detail: 'Another turn is active.'});
  clickRegenerate();
  await settle();
  check(!modelUnavailable && unavailableNotice.hidden, 'a busy regenerate shows no notice');
  check(!input.disabled && !submitBtn.disabled && !regenerate.disabled, 'and locks nothing');
  check(requests.length === 1, 'and reloads no catalog');
  check(errorEl.textContent === 'Another turn is active.', 'its own error is shown');

  // Reasoning, through the listener bound on its select.
  reset();
  reply(409, UNAVAILABLE);
  reply(200, CATALOG_WITHOUT);
  chooseReasoning('low');
  await settle();
  check(requests[0][0] === 'PATCH' && requests[0][1] === '/v2/api/chat/conversations/c-1/reasoning',
    'choosing a level asks to change reasoning');
  check(modelUnavailable && !unavailableNotice.hidden, 'a refused reasoning change shows the notice');
  check(input.disabled && submitBtn.disabled && reasoningSelect.disabled, 'and locks the composer and reasoning');
  check(requests.length === 2 && requests[1][1] === '/v2/api/chat/catalog', 'and reloads the catalog');

  reset();
  reply(409, {status_code: 409, detail: 'Model and reasoning can only change between turns.'});
  chooseReasoning('low');
  await settle();
  check(!modelUnavailable && unavailableNotice.hidden, 'a between-turns reasoning refusal shows no notice');
  check(!input.disabled && !reasoningSelect.disabled, 'and locks nothing');
  check(requests.length === 1, 'and reloads no catalog');
  check(reasoningSelect.value === 'high', 'the select goes back to the stored level');

  // The code alone is not enough: it has to arrive on a 409.
  check(!isModelUnavailable({status: 422, code: 'model_unavailable'}), 'a 422 with the code is not the refusal');
  check(!isModelUnavailable({status: 409, code: null}), 'a 409 without the code is not the refusal');
  check(isModelUnavailable({status: 409, code: 'model_unavailable'}), 'the refusal is recognised');
}

main().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});
