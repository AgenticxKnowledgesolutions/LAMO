document.addEventListener("DOMContentLoaded", function () {
    const toasts = document.querySelectorAll(".lamo-toast");

    toasts.forEach(function (toast) {
        const closeButton = toast.querySelector(".lamo-toast-close");
        let timer;

        function closeToast() {
            if (toast.classList.contains("is-closing")) return;

            clearTimeout(timer);
            toast.classList.add("is-closing");

            setTimeout(function () {
                toast.remove();
            }, 350);
        }

        closeButton.addEventListener("click", closeToast);

        timer = setTimeout(closeToast, 4500);

        /* Pause auto-close while the user is reading it */
        toast.addEventListener("mouseenter", function () {
            clearTimeout(timer);
            const progress = toast.querySelector(".lamo-toast-progress");
            if (progress) progress.style.animationPlayState = "paused";
        });

        toast.addEventListener("mouseleave", function () {
            const progress = toast.querySelector(".lamo-toast-progress");
            if (progress) progress.style.animationPlayState = "running";
            timer = setTimeout(closeToast, 1200);
        });
    });
});