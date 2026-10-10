/* =========================================================
   LAMO MESSAGE TOAST CONTROLS
========================================================= */

function closeLamoToast(button) {
  const toast = button.closest(".lamo-toast");

  if (!toast) {
    return;
  }

  toast.classList.add("toast-closing");

  setTimeout(function () {
    toast.remove();
  }, 250);
}

document.addEventListener("DOMContentLoaded", function () {
  const toasts =
    document.querySelectorAll(".lamo-toast");

  toasts.forEach(function (toast) {

    setTimeout(function () {
      if (!toast.isConnected) {
        return;
      }

      toast.classList.add("toast-closing");

      setTimeout(function () {
        toast.remove();
      }, 250);

    }, 5000);

  });
});