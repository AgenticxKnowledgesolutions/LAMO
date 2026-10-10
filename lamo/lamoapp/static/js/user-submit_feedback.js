document.addEventListener("DOMContentLoaded", function () {
    const stars = document.querySelectorAll("#starRating i");
    const ratingInput = document.getElementById("ratingInput");
    const ratingText = document.getElementById("ratingText");
    const ratingLabels = ["Poor", "Fair", "Good", "Very Good", "Excellent"];

    stars.forEach(function (star) {
        star.addEventListener("click", function () {
            const value = parseInt(this.dataset.value);
            ratingInput.value = value;

            stars.forEach(function (s, index) {
                if (index < value) {
                    s.classList.remove("bi-star");
                    s.classList.add("bi-star-fill", "active");
                } else {
                    s.classList.remove("bi-star-fill", "active");
                    s.classList.add("bi-star");
                }
            });

            ratingText.textContent = ratingLabels[value - 1];
        });

        star.addEventListener("mouseenter", function () {
            const value = parseInt(this.dataset.value);
            stars.forEach(function (s, index) {
                if (index < value) {
                    s.style.color = "#f0a500";
                } else {
                    s.style.color = "#dfece7";
                }
            });
        });
    });

    document.getElementById("starRating").addEventListener("mouseleave", function () {
        const currentRating = parseInt(ratingInput.value) || 0;
        stars.forEach(function (s, index) {
            if (index < currentRating) {
                s.style.color = "#f0a500";
            } else {
                s.style.color = "#dfece7";
            }
        });
    });

    const hasComplaint = document.getElementById("hasComplaint");
    const complaintFields = document.getElementById("complaintFields");

    hasComplaint.addEventListener("change", function () {
        if (this.checked) {
            complaintFields.classList.add("show");
        } else {
            complaintFields.classList.remove("show");
        }
    });
});