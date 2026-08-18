// Runs chat.js's threadBreaks over a reconcile snapshot under node.
//
// The reconcile sweeps away any divider it did not key, so a boundary this
// function drops is a divider that disappears from the stream — including the
// live one the person just watched appear. A Quick chat has no row for the
// thread it opens in, so its very first row is a boundary like any other, and
// that is the case a source-string assertion cannot pin.

function check(condition, description) {
  if (!condition) {
    console.error('FAIL: ' + description);
    process.exitCode = 1;
  }
}

// ── Function under test, spliced from chat.js ─────────────
// <CHAT_JS_FUNCTIONS>

var firstDrawn = {id: 'thread-1', sequence: 1, start_sequence: 5, title: 'Postgres pricing'};
var secondDrawn = {id: 'thread-2', sequence: 2, start_sequence: 11, title: 'Deploying the bot'};

var lone = threadBreaks({threads: [firstDrawn]});
check(lone[5] === firstDrawn, 'the first boundary a conversation ever draws is kept');
check(Object.keys(lone).length === 1, 'nothing else is keyed');

var both = threadBreaks({threads: [firstDrawn, secondDrawn]});
check(both[5] === firstDrawn, 'the first boundary is keyed by the message it sits above');
check(both[11] === secondDrawn, 'the second boundary is keyed by the message it sits above');

check(Object.keys(threadBreaks({})).length === 0, 'a snapshot with no threads keys nothing');
