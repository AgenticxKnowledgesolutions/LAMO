import os
import uuid

from django import forms
from django.core.validators import FileExtensionValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from .models import Service, WorkerProfile, UserProfile

# Inner filename parts that must never appear in an uploaded proof name
# (checked in addition to the final extension: payload.exe.pdf).
PROOF_DANGEROUS_INNER_PARTS = frozenset((
    'exe', 'bat', 'cmd', 'com', 'scr', 'msi', 'jar', 'dll', 'apk',
    'js', 'jse', 'vbs', 'ps1', 'sh', 'php', 'html', 'htm', 'svg',
    'cgi', 'pl', 'py', 'rb',
))


class ServiceForm(forms.ModelForm):

    class Meta:
        model = Service
        fields = [
            'name',
            'description',
            'price',
            'image',
            'is_active',
            'is_featured',
        ]
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Example: Plumber'
            }),
            'description': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 4,
                'placeholder': 'Enter service description'
            }),
            'price': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': 'Price (₹)',
                'min': '0',
                'step': '0.01',
            }),
            'image': forms.ClearableFileInput(attrs={
                'class': 'form-control'
            }),
            'is_active': forms.CheckboxInput(attrs={
                'class': 'form-check-input'
            }),
            'is_featured': forms.CheckboxInput(attrs={
                'class': 'form-check-input'
            }),
        }


class UserProfileForm(forms.ModelForm):

    class Meta:
        model = UserProfile
        fields = [
            'phone',
            'address',
            'district',
            'latitude',
            'longitude',
            'profile_image',
            'bio',
            'date_of_birth',
        ]
        widgets = {
            'phone': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Phone Number',
                'maxlength': '15',
                'inputmode': 'numeric',
            }),
            'address': forms.Textarea(attrs={
                'class': 'form-control',
                'placeholder': 'Full Address',
                'rows': 3,
            }),
            'district': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'District',
            }),
            'latitude': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': 'Latitude',
                'step': 'any',
            }),
            'longitude': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': 'Longitude',
                'step': 'any',
            }),
            'profile_image': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': '.jpg,.jpeg,.png,.webp',
            }),
            'bio': forms.Textarea(attrs={
                'class': 'form-control',
                'placeholder': 'Tell us about yourself...',
                'rows': 3,
            }),
            'date_of_birth': forms.DateInput(attrs={
                'class': 'form-control',
                'type': 'date',
            }),
        }
class WorkerProfileForm(forms.ModelForm):

    class Meta:
        model = WorkerProfile

        fields = [
            'phone',
            'service',
            'experience_years',
            'district',
            'availability',
            'pricing_type',
            'pricing_amount',
            'address',
            'latitude',
            'longitude',
            'proof_document',
            'profile_image',
        ]

        widgets = {

            'phone': forms.TextInput(attrs={
                'class': 'worker-input',
                'placeholder': 'Phone Number',
                'maxlength': '10',
                'inputmode': 'numeric',
            }),

            'service': forms.Select(attrs={
                'class': 'worker-select',
            }),

            'experience_years': forms.NumberInput(attrs={
                'class': 'worker-input',
                'placeholder': 'Experience (years)',
                'min': '0',
                'max': '60',
            }),

            'district': forms.Select(attrs={
                'class': 'worker-select',
            }),

            'availability': forms.Select(attrs={
                'class': 'worker-select',
            }),

            'pricing_type': forms.Select(attrs={
                'class': 'worker-select',
            }),

            'pricing_amount': forms.NumberInput(attrs={
                'class': 'worker-input',
                'placeholder': 'Amount (₹)',
                'min': '0',
                'step': '0.01',
            }),

            'address': forms.Textarea(attrs={
                'class': 'worker-textarea',
                'placeholder': 'Full Address',
                'rows': 3,
            }),

            'latitude': forms.NumberInput(attrs={
                'class': 'worker-input',
                'placeholder': 'Latitude',
                'step': 'any',
            }),

            'longitude': forms.NumberInput(attrs={
                'class': 'worker-input',
                'placeholder': 'Longitude',
                'step': 'any',
            }),

            'proof_document': forms.FileInput(attrs={
                'class': 'worker-file',
                'accept': '.pdf,.jpg,.jpeg,.png',
            }),

            'profile_image': forms.FileInput(attrs={
                'class': 'worker-file',
                'accept': '.jpg,.jpeg,.png,.webp',
            }),
        }

    def clean_phone(self):
        phone = self.cleaned_data.get('phone', '').strip()

        # Indian mobile number:
        # Must be exactly 10 digits
        # Must start with 6, 7, 8 or 9
        import re

        if not re.fullmatch(r'[6-9]\d{9}', phone):
            raise forms.ValidationError(
                'Please enter a valid 10-digit Indian mobile number.'
            )

        # Prevent duplicate phone numbers
        if WorkerProfile.objects.filter(
            phone=phone
        ).exclude(
            pk=self.instance.pk
        ).exists():

            raise forms.ValidationError(
                'This phone number is already registered.'
            )

        return phone

    def clean_experience_years(self):
        experience = self.cleaned_data.get(
            'experience_years'
        )

        if experience is None:
            raise forms.ValidationError(
                'Please enter your experience.'
            )

        if experience < 0:
            raise forms.ValidationError(
                'Experience cannot be negative.'
            )

        if experience > 60:
            raise forms.ValidationError(
                'Experience cannot exceed 60 years.'
            )

        return experience

    def clean_proof_document(self):
        proof = self.cleaned_data.get('proof_document')
        if not proof:
            raise forms.ValidationError('Please upload your proof document.')

        # ---- Extension whitelist (applied first; cheap) ----
        from django.core.validators import FileExtensionValidator
        FileExtensionValidator(allowed_extensions=['pdf', 'jpg', 'jpeg', 'png'])(proof)

        # ---- Path traversal / directory-component rejection ----
        base_name = os.path.basename(proof.name)
        if base_name != proof.name or not base_name:
            raise forms.ValidationError('Invalid file name.')

        # ---- Double-extension rejection (document.pdf.exe, document.exe.pdf) ----
        root, ext = os.path.splitext(base_name)
        if ext.lower() not in ('.pdf', '.jpg', '.jpeg', '.png'):
            raise forms.ValidationError(
                'Proof must be a PDF, JPG, JPEG or PNG file.',
            )
        # Dangerous inner parts (payload.exe.pdf) even when the final
        # extension is allowed.
        inner_parts = root.lower().split('.')
        if any(part in PROOF_DANGEROUS_INNER_PARTS for part in inner_parts):
            raise forms.ValidationError(
                'Proof must be a PDF, JPG, JPEG or PNG file.',
            )

        # ---- MIME / content-type whitelist ----
        if proof.content_type not in (
            'application/pdf',
            'image/jpeg',
            'image/jpg',
            'image/png',
        ):
            raise forms.ValidationError(
                'Unsupported file type. Only PDF, JPG, JPEG or PNG allowed.',
            )

        # ---- Magic-bytes (content) verification to prevent MIME spoofing ----
        head = proof.read(8)
        proof.seek(0)
        if head.startswith(b'%PDF'):
            expected_mime = 'application/pdf'
        elif head.startswith(b'\xFF\xD8\xFF'):
            expected_mime = 'image/jpeg'
        elif head.startswith(b'\x89PNG\r\n\x1a\n'):
            expected_mime = 'image/png'
        else:
            raise forms.ValidationError(
                'File content does not match the declared file type.',
            )

        # ---- Enforce size limit ----
        if proof.size > 5 * 1024 * 1024:
            raise forms.ValidationError(
                'Proof document must be less than 5 MB.',
            )

        # ---- Store under a random, directory-free name ----
        # (upload_to='worker_proofs/' is prepended by the field itself,
        # so the name must not repeat the directory.)
        safe_name = f'{uuid.uuid4().hex}{ext.lower()}'
        proof.name = safe_name

        return proof

    def clean_profile_image(self):
        image = self.cleaned_data.get(
            'profile_image'
        )

        # Profile image is optional
        if not image:
            return image

        allowed_extensions = (
            '.jpg',
            '.jpeg',
            '.png',
            '.webp',
        )

        file_name = image.name.lower()

        if not file_name.endswith(
            allowed_extensions
        ):
            raise forms.ValidationError(
                'Profile image must be JPG, JPEG, PNG or WEBP.'
            )

        # Maximum 5 MB
        if image.size > 5 * 1024 * 1024:
            raise forms.ValidationError(
                'Profile image must be less than 5 MB.'
            )

        return image