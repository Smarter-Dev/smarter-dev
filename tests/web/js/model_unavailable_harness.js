// Runs chat.js's setBusy and setModelAvailability over a stub DOM under node.
//
// A conversation whose model was retired keeps it; the composer is what stops
// it being used. So the lock has to hold through every later setBusy call (a
// turn finishing, an upload settling), and lift only when the notice does.

function check(condition, description) {
  if (!condition) {
    console.error('FAIL: ' + description);
    process.exitCode = 1;
  }
}

function element(extra) {
  var el = {disabled: false, hidden: false, textContent: '', dataset: {}};
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
var shell = element();
var input = element();
var submitBtn = element();
var form = element();
var hintEl = element();
var stopBtn = element();
var modelSelect = element();
var reasoningSelect = element();
var regenerate = element();

var groups = {
  '[data-regenerate]': [regenerate],
  '[data-chat-model]': [modelSelect],
  '[data-chat-reasoning]': [reasoningSelect],
};
var document = {
  querySelectorAll: function (selector) { return groups[selector] || []; },
};

var IDLE_HINT = 'shift+enter · newline';
var UNAVAILABLE_HINT = 'Choose an available model to continue';
var uploadCount = 0;
var activeTurn = null;
var conversationId = 'c-1';
var modelUnavailable = false;

// ── Functions under test, spliced from chat.js ────────────
// <CHAT_JS_FUNCTIONS>

setModelAvailability(false, 'gpt-5-4');
check(!unavailableNotice.hidden, 'the notice shows for a retired selection');
check(keyName.textContent === 'gpt-5-4', 'the notice names the stored key');
check(input.disabled, 'the message box is disabled');
check(submitBtn.disabled, 'Send is disabled');
check(regenerate.disabled, 'Regenerate is disabled');
check(reasoningSelect.disabled, 'reasoning cannot be changed on a retired model');
check(!modelSelect.disabled, 'the model select stays usable: it is the way out');
check(hintEl.textContent === UNAVAILABLE_HINT, 'the hint says why Send is off');
check(shell.dataset.modelAvailable === 'false', 'the shell records the state');

setBusy(false);
check(input.disabled && submitBtn.disabled, 'a turn finishing does not unlock the composer');

setModelAvailability(true, 'gpt-6-luna');
check(unavailableNotice.hidden, 'the notice goes once an available model is confirmed');
check(!input.disabled, 'the message box is enabled again');
check(!submitBtn.disabled, 'Send is enabled again');
check(!regenerate.disabled, 'Regenerate is enabled again');
check(!reasoningSelect.disabled, 'reasoning is enabled again');
check(hintEl.textContent === IDLE_HINT, 'the idle hint returns');

setBusy(true);
check(submitBtn.disabled && !input.disabled, 'an ordinary running turn still only locks Send');
