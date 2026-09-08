/*
 * Anastomosis GUI — the one teach scaffold both Teach modes run on.
 *
 * Teaching a document layout and teaching an export format are the same two
 * steps, gated the way the CLI gates them: "look" calls the flow's async
 * starter with confirmed=false, the controller refuses to write and stashes
 * what it found; the confirmation arms the write, which calls the same starter
 * with confirmed=true. Both are fire-and-forget — the call returns
 * {started:true} and the real result arrives via the shell's dispatcher, which
 * is why every terminal event fetches the stashed result.
 *
 * A descriptor carries what actually differs: the stage name, the element ids,
 * the two bridge calls, the step sentences, and the callback that paints the
 * proposal. Everything else is here, once.
 */
"use strict";

(function () {
  const Shell = window.AnastShell;
  const el = (id) => document.getElementById(id);

  //: One teach mode, wired. Returns the handles the owning view needs back:
  //: its `onEvent` to register, and the two painters the descriptor's own
  //: refusal handling re-enters through.
  function wizard(spec) {
    const say = spec.say;
    // Every control of a mode is `<mode>-<part>` in the markup. The two named
    // for what the mode does rather than which part they are — the thing being
    // learned from, and the verb on the write button — ride the descriptor.
    const id = (part) => `${spec.mode}-${part}`;

    // The step line is the whole of what a click on "look" answers, so it is
    // said as well as shown.
    function setStep(text) {
      Shell.setStatus(el(id("step")), text);
    }

    //: Held from the click until the run's terminal event.
    //:
    //: NOT `Shell.guardButton`: that releases when its `work` resolves, and the
    //: starter resolves as soon as the WORKER STARTS. Three rapid clicks still
    //: fired three analyses through it — measured, not assumed. The only
    //: on-screen feedback here is the step line, which is not a live region, so
    //: a screen-reader operator gets nothing from a click and will reasonably
    //: click again.
    let analyzeLabel = "";
    function setAnalyzing(busy) {
      const button = el(id("analyze"));
      if (!button) return;
      // Remember the button's OWN label rather than re-typing it here, so the
      // markup stays the single place the wording lives.
      if (!analyzeLabel) analyzeLabel = button.textContent;
      button.disabled = busy;
      button.textContent = busy ? "Looking…" : analyzeLabel;
    }

    function paintProposal(res) {
      spec.renderProposal(res);
      el(id("proposal")).hidden = false;
      // Consent is per-analysis, never sticky: a fresh look re-arms the gate.
      el(id("confirm")).checked = false;
      el(spec.write).disabled = true;
      setStep(say.review);
    }

    function showResult(where, body) {
      el(id("result-path")).textContent = where;
      el(id("result-md")).textContent = body || "";
      el(id("result")).hidden = false;
    }

    // ConfirmationRequired is the EXPECTED outcome of step 1 (it carries what
    // there is to confirm); ok is the written thing; anything the mode does not
    // claim as a pointed refusal is a failure.
    function route(res) {
      if (res && res.ok) {
        spec.renderWritten(res, { showResult, setStep });
      } else if (res && res.error === "ConfirmationRequired") {
        paintProposal(res);
      } else if (!(spec.onRefusal && spec.onRefusal(res))) {
        Shell.showBanner(`${say.failed}: ${res ? res.error : "no answer from the app"}`);
      }
    }

    async function fetchResult() {
      if (!Shell.hasApi()) return;
      try {
        route(await spec.last());
      } catch (err) {
        setAnalyzing(false);
        Shell.showBanner(String(err));
      }
    }

    // The terminal stage fetches the stashed result, which carries the
    // outcome-specific detail a bare error string does not. What an `error`
    // event does is the mode's call: a mode with pointed refusals wants the
    // stash, one without it wants the event's own sentence.
    function onEvent(event) {
      if (event.type === "done" || event.type === "error" || event.state === "done") {
        setAnalyzing(false);
      }
      if (event.type === "stage" && event.stage === spec.stage && event.state === "done") {
        fetchResult();
      } else if (event.type === "done") {
        fetchResult();
      } else if (event.type === "error") {
        if (spec.onErrorEvent) spec.onErrorEvent(event);
        else fetchResult();
      }
    }

    function values() {
      return {
        input: el(spec.input).value,
        name: el(id("name")).value,
        display: el(id("display")).value || null,
      };
    }

    async function onAnalyze() {
      if (!Shell.hasApi()) return;
      Shell.hideBanner();
      el(id("result")).hidden = true;
      const v = values();
      if (
        !Shell.requireFields([
          [v.input, spec.needs.input, spec.input],
          [v.name, spec.needs.name, id("name")],
        ])
      ) {
        return;
      }
      setStep(say.looking);
      setAnalyzing(true);
      try {
        // Looking again re-runs the reading over the same input, so it carries
        // no review: there is nothing yet for one to correct.
        const started = await spec.start(v, false);
        if (started && started.ok === false) {
          Shell.showBanner(Shell.refusalText(started.error, say.unreadable));
          setStep(say.look);
          setAnalyzing(false);
        }
      } catch (err) {
        Shell.showBanner(String(err));
      }
    }

    async function onWrite() {
      if (!Shell.hasApi() || !el(id("confirm")).checked) return;
      Shell.hideBanner();
      if (spec.beforeWrite && !spec.beforeWrite()) return;
      setStep(say.writing);
      try {
        const started = await spec.start(values(), true);
        if (started && started.ok === false) {
          Shell.showBanner(Shell.refusalText(started.error, say.unwritable));
          setStep(say.review);
        }
      } catch (err) {
        Shell.showBanner(String(err));
      }
    }

    el(id("analyze")).addEventListener("click", onAnalyze);
    el(spec.write).addEventListener("click", onWrite);
    el(id("confirm")).addEventListener("change", () => {
      el(spec.write).disabled = !el(id("confirm")).checked;
    });
    Shell.onReady((live) => {
      el(id("analyze")).disabled = !live;
    });

    return { onEvent, paintProposal, setStep, setAnalyzing };
  }

  window.AnastLearn = { wizard };
})();
