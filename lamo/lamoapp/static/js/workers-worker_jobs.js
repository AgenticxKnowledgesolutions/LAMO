document.addEventListener("DOMContentLoaded", function () {

    const messages = document.querySelectorAll(".lamo-message");

    messages.forEach(function (message) {

        setTimeout(function () {

            closeMessageElement(message);

        }, 5000);

    });

});


function closeMessage(button) {

    const message = button.closest(".lamo-message");

    closeMessageElement(message);

}


function closeMessageElement(message) {

    if (!message) {
        return;
    }

    message.classList.add("hide");

    setTimeout(function () {

        if (message && message.parentNode) {

            message.parentNode.removeChild(message);

        }

    }, 350);

}