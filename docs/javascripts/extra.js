// Code is highlighted at build time. Add a readable language label to blocks
// that have no filename, without changing the copied source.
(() => {
  const languages = {
    sh: "Shell", bash: "Shell", shell: "Shell", console: "Terminal",
    python: "Python", py: "Python", json: "JSON", yaml: "YAML", toml: "TOML",
    text: "Text", js: "JavaScript", javascript: "JavaScript",
  };
  function labelCode() {
    document.querySelectorAll(".highlight").forEach((block) => {
      if (block.querySelector(":scope > .filename") || block.dataset.labelled) return;
      const language = [...block.classList].find((name) => name.startsWith("language-"))?.slice(9);
      if (!language) return;
      const label = document.createElement("span");
      label.className = "filename";
      label.textContent = languages[language] || language;
      block.prepend(label);
      block.dataset.labelled = "true";
    });
  }
  labelCode();
  if (typeof document$ !== "undefined") document$.subscribe(labelCode);
})();
