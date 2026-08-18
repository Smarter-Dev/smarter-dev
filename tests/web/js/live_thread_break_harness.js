// Runs chat.js's live-divider functions against a stub DOM under node.
//
// The theme has no browser test runner, and the live divider is the one piece of
// stage 03 that only exists in the browser: a source-string assertion cannot tell
// an anchor selector that matches from one that never can. So the three functions
// under test are spliced out of chat.js by name (see quick_chat_thread_tool_test.py)
// and dropped in below, over a stub that understands exactly the selectors they
// use: one class token plus any number of [data-*] attribute filters.

function camelCaseAttribute(attributeName) {
  return attributeName
    .replace(/^data-/, '')
    .replace(/-([a-z])/g, function (_, letter) { return letter.toUpperCase(); });
}

function elementMatchesSelector(element, selector) {
  var classToken = selector.match(/^\.([A-Za-z0-9_-]+)/);
  if (classToken && String(element.className).split(/\s+/).indexOf(classToken[1]) === -1) return false;
  var attributeFilter = /\[([a-z-]+)(?:="([^"]*)")?\]/g;
  var filter;
  while ((filter = attributeFilter.exec(selector)) !== null) {
    var value = element.dataset[camelCaseAttribute(filter[1])];
    if (value === undefined) return false;
    if (filter[2] !== undefined && String(value) !== filter[2]) return false;
  }
  return true;
}

function descendantsOf(element) {
  var found = [];
  element.children.forEach(function (child) {
    found.push(child);
    found = found.concat(descendantsOf(child));
  });
  return found;
}

function createStubElement(tagName) {
  return {
    tagName: tagName,
    className: '',
    dataset: {},
    attributes: {},
    children: [],
    textContent: '',
    setAttribute: function (name, value) { this.attributes[name] = value; },
    appendChild: function (child) { this.children.push(child); return child; },
    insertBefore: function (child, reference) {
      var at = reference ? this.children.indexOf(reference) : -1;
      if (at === -1) this.children.push(child);
      else this.children.splice(at, 0, child);
      return child;
    },
    querySelectorAll: function (selector) {
      return descendantsOf(this).filter(function (element) {
        return elementMatchesSelector(element, selector);
      });
    },
    querySelector: function (selector) {
      return this.querySelectorAll(selector)[0] || null;
    }
  };
}

var document = {createElement: createStubElement};
var mode = 'chat';
var thread = createStubElement('div');
var statusEl = null;

function appendMessage(role, turnId) {
  var article = createStubElement('article');
  article.className = 'chat-message chat-message-' + role;
  if (turnId) article.dataset.turnId = turnId;
  thread.appendChild(article);
  return article;
}

function labelOf(divider) {
  return divider.querySelector('.chat-thread-break-label').textContent;
}

function check(condition, description) {
  if (!condition) {
    console.error('FAIL: ' + description);
    process.exitCode = 1;
  }
}

// ── Functions under test, spliced from chat.js ────────────
// <CHAT_JS_FUNCTIONS>

// 1. The sender's own browser mid-turn: the optimistic user article was created
//    before the POST named the turn, so it may carry no turn id at all. This is
//    the case the live divider exists to serve.
var unnamedUserArticle = appendMessage('user', null);
var pendingAssistantArticle = appendMessage('assistant', 'turn-1');
insertLiveThreadBreak({
  thread_id: 'thread-1',
  turn_id: 'turn-1',
  start_sequence: 4,
  title: 'Deploying the bot'
});
var divider = thread.querySelector('[data-thread-break]');
check(divider !== null, 'a divider is inserted for an unnamed user article');
check(
  divider && thread.children.indexOf(divider) === thread.children.indexOf(unnamedUserArticle) - 1,
  'the divider sits directly above the message being answered'
);
check(divider && divider.dataset.threadId === 'thread-1', 'the divider carries the thread id');
check(divider && String(divider.dataset.startSequence) === '4', 'the divider carries the start sequence');
check(divider && labelOf(divider) === 'Deploying the bot', 'the divider is labelled with the thread title');
check(pendingAssistantArticle !== null, 'the assistant placeholder is untouched');

// 2. A second notification for the same thread changes nothing.
insertLiveThreadBreak({
  thread_id: 'thread-1',
  turn_id: 'turn-1',
  start_sequence: 4,
  title: 'Deploying the bot'
});
check(thread.querySelectorAll('[data-thread-break]').length === 1, 'the same thread never inserts twice');

// 3. With turn ids on the articles, the divider anchors on the turn being
//    answered rather than on whichever user message happens to be last.
thread = createStubElement('div');
appendMessage('user', 'turn-1');
appendMessage('assistant', 'turn-1');
var answeredUserArticle = appendMessage('user', 'turn-2');
appendMessage('assistant', 'turn-2');
insertLiveThreadBreak({
  thread_id: 'thread-2',
  turn_id: 'turn-2',
  start_sequence: 6,
  title: 'A new subject'
});
var secondDivider = thread.querySelector('[data-thread-break]');
check(secondDivider !== null, 'a divider is inserted for a named user article');
check(
  secondDivider && thread.children.indexOf(secondDivider) === thread.children.indexOf(answeredUserArticle) - 1,
  'the divider anchors on the turn named by the notification'
);

// 4. A notification with no thread id is ignored.
thread = createStubElement('div');
appendMessage('user', null);
insertLiveThreadBreak({turn_id: 'turn-1', start_sequence: 2, title: 'Nothing'});
check(thread.querySelectorAll('[data-thread-break]').length === 0, 'a thread id is required');
