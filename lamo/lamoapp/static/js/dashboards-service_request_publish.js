function previewImage(input) {
    const preview = document.getElementById('imagePreview');
    const uploadText = document.getElementById('uploadText');

    if (input.files && input.files[0]) {
        const reader = new FileReader();

        reader.onload = function (e) {
            preview.src = e.target.result;
            preview.classList.add('visible');
            if (uploadText) {
                uploadText.textContent = 'Image selected: ' + input.files[0].name;
            }
        };

        reader.readAsDataURL(input.files[0]);
    } else {
        preview.src = '';
        preview.classList.remove('visible');
        if (uploadText) {
            uploadText.textContent = 'Click to upload service image';
        }
    }
}