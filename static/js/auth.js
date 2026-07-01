document.querySelectorAll("[data-toggle-password]").forEach(function (btn) {
    btn.addEventListener("click", function () {
        var input = document.getElementById(btn.getAttribute("data-toggle-password"));
        if (!input) return;
        var icon = btn.querySelector(".material-symbols-outlined");
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        if (icon) icon.textContent = show ? "visibility_off" : "visibility";
        btn.setAttribute("aria-label", show ? "Ocultar senha" : "Mostrar senha");
    });
});
