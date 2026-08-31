// Upload outcome messaging — the missing half of the upload UX.
//
// The 202 path already talks: the route returns the re-queried document list
// partial and the form swaps it into #document-list (D28's upload half, closed
// in b79fc06). But htmx does not swap 4xx/5xx responses by default, so a 409
// duplicate, a 413 too-large or a 400 bad request landed silently — the user
// saw nothing and assumed success (found in manual testing, 2026-08-31).
//
// This listener renders the outcome into #upload-status: the JSON `detail`
// the route already produces for every error, or a success line for the 202.
// Scoped to the upload form alone — the list's polling GETs must not touch it.
document.addEventListener("htmx:afterRequest", function (event) {
    var elt = event.detail.elt;
    if (!elt || elt.id !== "upload-form") return;

    var box = document.getElementById("upload-status");
    if (!box) return;

    var xhr = event.detail.xhr;
    if (!xhr) return;

    function show(message, ok) {
        box.textContent = message;
        box.className = "upload-status " + (ok ? "upload-status-ok" : "upload-status-error");
    }

    if (event.detail.successful) {
        show("Upload accepted — ingestion queued. The list below tracks its progress.", true);
        return;
    }

    var message = "Upload failed (" + xhr.status + ").";
    try {
        var body = JSON.parse(xhr.responseText);
        if (body && body.detail) message = body.detail;
    } catch (e) {
        // Not JSON — keep the status-code message.
    }
    show(message, false);
});
