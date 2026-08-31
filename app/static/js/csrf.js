document.addEventListener("htmx:configRequest", function (event) {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
    if (match) {
        event.detail.headers["x-csrftoken"] = decodeURIComponent(match[1]);
    }
});
