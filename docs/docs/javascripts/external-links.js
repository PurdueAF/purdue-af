// Links in the navigation that leave the site open in a new tab.
document.addEventListener(
  "click",
  (event) => {
    const link = event.target.closest(".md-nav a[href]");
    if (link && link.host !== location.host) {
      event.preventDefault();
      window.open(link.href, "_blank", "noopener");
    }
  },
  true,
);
