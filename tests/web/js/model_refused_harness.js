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

var document = {
  addEventListener: function () {},
  querySelectorAll: function () { return []; },
  querySelector: function () { return null; },
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
var pendingAttachments = [];
var autoGrown = 0;
var activations = [];

function json(method, body) { return {method: method, body: JSON.stringify(body)}; }
function setStatus() {}
function scrollToBottom() {}
function autoGrow() { autoGrown += 1; }
function submitResources() { throw new Error('not in chat mode'); }
function submissionKey() { return 'k'; }
function createConversation() { throw new Error('the conversation exists'); }
// The real one rebuilds the selects and ends by deriving availability from the
// catalog it was given; only that last step matters here.
function activateConversationControls(persistedModel, persistedReasoning) {
  activations.push([persistedModel, persistedReasoning]);
  setModelAvailability(
    catalog.models.some(function (item) { return item.key === persistedModel; }),
    persistedModel
  );
}

var requests = [];
var replies = [];
function reply(status, body) { replies.push({status: status, body: body}); }
function fetch(url, options) {
  requests.push([options && options.method || 'GET', url]);
  var next = replies.shift();
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

function reset() {
  thread.children = [];
  requests = [];
  activations = [];
  autoGrown = 0;
  input.value = '';
  setModelAvailability(true, 'gpt-6-1-sol');
  showError('');
}

var UNAVAILABLE = {status_code: 409, detail: 'The selected model is unavailable; choose a new model.', code: 'model_unavailable'};
var CATALOG_WITHOUT = {models: [{key: 'gpt-6-luna'}]};
var CATALOG_WITH = {models: [{key: 'gpt-6-luna'}, {key: 'gpt-6-1-sol'}]};

async function main() {
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
  check(activations.length === 1 && activations[0][0] === 'gpt-6-1-sol' && activations[0][1] === 'high',
    'the controls are rebuilt on the stored selection');
  check(sendMessage('again') === false, 'a second send is stopped before the network');
  check(requests.length === 2, 'and makes no request');

  // Something typed while the request was out is not overwritten.
  reset();
  reply(409, UNAVAILABLE);
  reply(200, CATALOG_WITHOUT);
  submit('First draft');
  input.value = 'Typed since';
  await settle();
  check(input.value === 'Typed since', 'a newer draft wins over the returned one');
  check(modelUnavailable, 'the composer still locks');

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
  check(thread.children[1].content.textContent === 'Wait for the active turn to finish or stop it.',
    'and shows its error in place, as before');

  // The code alone is not enough: it has to arrive on a 409.
  check(!isModelUnavailable({status: 422, code: 'model_unavailable'}), 'a 422 with the code is not the refusal');
  check(!isModelUnavailable({status: 409, code: null}), 'a 409 without the code is not the refusal');
  check(isModelUnavailable({status: 409, code: 'model_unavailable'}), 'the refusal is recognised');
}

main().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});
