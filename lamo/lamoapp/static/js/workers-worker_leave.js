document.addEventListener("DOMContentLoaded", function () {
    const today = new Date().toISOString().split('T')[0];
    const leaveInput = document.getElementById('leave_date');
    if (leaveInput) {
        leaveInput.setAttribute('min', today);
    }
});