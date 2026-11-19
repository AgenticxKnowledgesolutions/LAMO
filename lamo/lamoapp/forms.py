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
            'commission_type',
            'commission_value',
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
            'commission_type': forms.Select(attrs={
                'class': 'form-control',
            }),
            'commission_value': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. 8 (%) or 40 (₹)',
                'min': '0',
                'step': '0.01',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Older forms/clients that do not send these fields keep the
        # platform default commission.
        self.fields['commission_type'].required = False
        self.fields['commission_value'].required = False

    def clean_commission_type(self):
        return self.cleaned_data.get('commission_type') or 'DEFAULT'

    def clean(self):
        cleaned = super().clean()
        c_type = cleaned.get('commission_type') or 'DEFAULT'
        c_value = cleaned.get('commission_value')
        if c_type in ('PERCENT', 'FIXED'):
            if c_value is None:
                self.add_error('commission_value', 'Enter the commission value for this commission type.')
            elif c_value < 0:
                self.add_error('commission_value', 'Commission cannot be negative.')
            elif c_type == 'PERCENT' and c_value > 100:
                self.add_error('commission_value', 'A percentage commission cannot exceed 100%.')
        return cleaned


class UserProfileForm(forms.ModelForm):
    first_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'First Name',
        })
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Last Name',
        })
    )

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
                'placeholder': 'House / Flat Name, Street, Landmark',
                'rows': 3,
            }),
            'district': forms.Select(
                choices=[('', 'Select District')] + [(d, d) for d, _ in WorkerProfile.DISTRICT_CHOICES],
                attrs={'class': 'form-control'}
            ),
            'latitude': forms.HiddenInput(),
            'longitude': forms.HiddenInput(),
            'profile_image': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'id': 'id_profile_image',
                'accept': '.jpg,.jpeg,.png,.webp',
            }),
            'bio': forms.Textarea(attrs={
                'class': 'form-control',
                'placeholder': 'Tell us about yourself...',
                'rows': 2,
            }),
            'date_of_birth': forms.DateInput(attrs={
                'class': 'form-control',
                'type': 'date',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and hasattr(self.instance, 'user') and self.instance.user:
            self.fields['first_name'].initial = self.instance.user.first_name
            self.fields['last_name'].initial = self.instance.user.last_name

    def save(self, commit=True):
        profile = super().save(commit=commit)
        user = getattr(profile, 'user', None)
        if user:
            first_name = self.cleaned_data.get('first_name', '').strip()
            last_name = self.cleaned_data.get('last_name', '').strip()
            user_dirty = False
            if first_name != user.first_name:
                user.first_name = first_name
                user_dirty = True
            if last_name != user.last_name:
                user.last_name = last_name
                user_dirty = True
            if user_dirty:
                user.save(update_fields=['first_name', 'last_name'])
        return profile
    def clean_profile_image(self):
        image = self.cleaned_data.get('profile_image')
        # False = "clear" checkbox, None = nothing submitted, FieldFile = unchanged.
        if not image or not hasattr(image, 'content_type'):
            return image
        from .upload_validation import validate_profile_photo
        validate_profile_photo(image)
        return image

    def clean_phone(self):
        phone = self.cleaned_data.get('phone', '').strip()
        if not phone:
            return ''
        import re
        from .views import _normalize_phone, _user_ids_for_phone
        clean_p = _normalize_phone(phone)
        if not clean_p:
            raise forms.ValidationError('Please enter a valid 10-digit Indian mobile number.')

        current_user_id = self.instance.user_id if self.instance and self.instance.pk else None
        other_user_ids = _user_ids_for_phone(clean_p)
        if current_user_id:
            other_user_ids.discard(current_user_id)
        if other_user_ids:
            raise forms.ValidationError('This phone number is already registered with another account.')
        return clean_p

    def clean_latitude(self):
        lat = self.cleaned_data.get('latitude')
        if lat is not None:
            try:
                lat_val = float(lat)
                if lat_val < -90.0 or lat_val > 90.0:
                    raise forms.ValidationError('Latitude must be between -90 and 90 degrees.')
            except (ValueError, TypeError):
                raise forms.ValidationError('Invalid latitude coordinate.')
        return lat

    def clean_longitude(self):
        lng = self.cleaned_data.get('longitude')
        if lng is not None:
            try:
                lng_val = float(lng)
                if lng_val < -180.0 or lng_val > 180.0:
                    raise forms.ValidationError('Longitude must be between -180 and 180 degrees.')
            except (ValueError, TypeError):
                raise forms.ValidationError('Invalid longitude coordinate.')
        return lng

TIME_CHOICES_12H = [
    ('06:00', '06:00 AM'),
    ('06:30', '06:30 AM'),
    ('07:00', '07:00 AM'),
    ('07:30', '07:30 AM'),
    ('08:00', '08:00 AM'),
    ('08:30', '08:30 AM'),
    ('09:00', '09:00 AM (Standard Start)'),
    ('09:30', '09:30 AM'),
    ('10:00', '10:00 AM'),
    ('10:30', '10:30 AM'),
    ('11:00', '11:00 AM'),
    ('11:30', '11:30 AM'),
    ('12:00', '12:00 PM (Noon)'),
    ('12:30', '12:30 PM'),
    ('13:00', '01:00 PM'),
    ('13:30', '01:30 PM'),
    ('14:00', '02:00 PM'),
    ('14:30', '02:30 PM'),
    ('15:00', '03:00 PM'),
    ('15:30', '03:30 PM'),
    ('16:00', '04:00 PM'),
    ('16:30', '04:30 PM'),
    ('17:00', '05:00 PM'),
    ('17:30', '05:30 PM'),
    ('18:00', '06:00 PM (Standard End)'),
    ('18:30', '06:30 PM'),
    ('19:00', '07:00 PM'),
    ('19:30', '07:30 PM'),
    ('20:00', '08:00 PM'),
    ('20:30', '08:30 PM'),
    ('21:00', '09:00 PM'),
    ('21:30', '09:30 PM'),
    ('22:00', '10:00 PM'),
    ('22:30', '10:30 PM'),
    ('23:00', '11:00 PM'),
]

class WorkerProfileForm(forms.ModelForm):

    work_start_time = forms.ChoiceField(
        choices=TIME_CHOICES_12H,
        required=False,
        widget=forms.Select(attrs={
            'class': 'worker-select',
            'id': 'id_work_start_time',
        }),
        help_text="Daily Shift Starts At"
    )
    work_end_time = forms.ChoiceField(
        choices=TIME_CHOICES_12H,
        required=False,
        widget=forms.Select(attrs={
            'class': 'worker-select',
            'id': 'id_work_end_time',
        }),
        help_text="Daily Shift Ends At"
    )

    class Meta:
        model = WorkerProfile

        fields = [
            'phone',
            'upi_id',
            'service',
            'experience_years',
            'district',
            'availability',
            'work_start_time',
            'work_end_time',
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
            'upi_id': forms.TextInput(attrs={
                'class': 'worker-input',
                'placeholder': 'UPI ID (e.g. 9876543210@paytm or name@okaxis)',
                'maxlength': '100',
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

            'latitude': forms.HiddenInput(),
            'longitude': forms.HiddenInput(),

            'proof_document': forms.FileInput(attrs={
                'class': 'worker-file',
                'accept': '.pdf,.jpg,.jpeg,.png',
            }),

            'profile_image': forms.FileInput(attrs={
                'class': 'worker-file',
                'accept': '.jpg,.jpeg,.png,.webp',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            if self.instance.work_start_time:
                self.initial['work_start_time'] = self.instance.work_start_time.strftime('%H:%M') if hasattr(self.instance.work_start_time, 'strftime') else str(self.instance.work_start_time)[:5]
            if self.instance.work_end_time:
                self.initial['work_end_time'] = self.instance.work_end_time.strftime('%H:%M') if hasattr(self.instance.work_end_time, 'strftime') else str(self.instance.work_end_time)[:5]

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get('work_start_time')
        end = cleaned_data.get('work_end_time')
        if start and end:
            try:
                s_h, s_m = map(int, str(start).split(':')[:2])
                e_h, e_m = map(int, str(end).split(':')[:2])
                if (e_h * 60 + e_m) <= (s_h * 60 + s_m):
                    self.add_error('work_end_time', 'Daily Shift End Time must be later than Start Time.')
            except Exception:
                pass
        return cleaned_data

    def clean_phone(self):
        phone = self.cleaned_data.get('phone', '').strip()
        import re
        from .views import _normalize_phone, _user_ids_for_phone

        clean_p = _normalize_phone(phone)
        if not clean_p:
            raise forms.ValidationError(
                'Please enter a valid 10-digit Indian mobile number.'
            )

        # Cross-role uniqueness check across both WorkerProfile and UserProfile
        current_user_id = self.instance.user_id if self.instance and self.instance.pk else None
        other_user_ids = _user_ids_for_phone(clean_p)
        if current_user_id:
            other_user_ids.discard(current_user_id)
        if other_user_ids:
            raise forms.ValidationError(
                'This phone number is already registered with another account.'
            )

        return clean_p

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
            if self.instance and self.instance.pk and self.instance.proof_document:
                return self.instance.proof_document
            raise forms.ValidationError('Please upload your proof document.')

        if self.instance and self.instance.pk and proof == self.instance.proof_document:
            return proof

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

        # ---- Content validation: the bytes must really be a valid PDF/JPEG/PNG ----
        # (decoded with Pillow / structurally checked; corrupt or disguised files
        # are refused). Also enforces the configurable size limit.
        from .upload_validation import validate_proof_document
        proof.name = base_name  # validator re-checks the original name safely
        validate_proof_document(proof)

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

        # Content validation (magic bytes + full decode). Skipped for an
        # unchanged, already-stored image.
        if hasattr(image, 'content_type'):
            from .upload_validation import validate_profile_photo
            validate_profile_photo(image)

        return image
