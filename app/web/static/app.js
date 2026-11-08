// Comportamentos do painel. Fica em arquivo próprio (sem onclick no HTML) para a
// política de segurança (CSP) só permitir scripts do próprio site.
(function () {
  "use strict";

  // Confirmação antes de ações sensíveis: <form data-confirm="..."> ou <button data-confirm="...">.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-confirm]");
    if (button && !window.confirm(button.dataset.confirm)) event.preventDefault();
  });
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.matches("form[data-confirm]") && !window.confirm(form.dataset.confirm)) event.preventDefault();
  });

  // Filtros que se aplicam sozinhos ao trocar a opção.
  document.addEventListener("change", function (event) {
    if (event.target.matches("[data-autosubmit]")) event.target.form.requestSubmit();
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (target.matches("[data-select-all]")) target.select();

    var opener = target.closest("[data-open-dialog]");
    if (opener) document.getElementById(opener.dataset.openDialog).showModal();

    var closer = target.closest("[data-close-dialog]");
    if (closer) closer.closest("dialog").close();
    if (target.matches("dialog[data-backdrop-close]")) target.close();

    var menu = target.closest("[data-menu-toggle]");
    if (menu) {
      var open = document.getElementById(menu.dataset.menuToggle).classList.toggle("open");
      menu.setAttribute("aria-expanded", open ? "true" : "false");
    }

    if (target.closest("[data-theme-toggle]")) {
      var root = document.documentElement;
      var dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("tema", root.dataset.theme); } catch (e) {}
      if (window.Chart) location.reload(); // os gráficos leem as cores ao abrir
    }
  });
})();
