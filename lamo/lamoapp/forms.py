from django import forms
from .models import Service, WorkerProfile, UserProfile


class ServiceForm(forms.ModelForm):

    class Meta:
        model = Service
        fields = [
            'name',
            'description',
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

            'proof_document': forms.ClearableFileInput(attrs={
                'class': 'worker-file',
                'accept': '.pdf,.jpg,.jpeg,.png',
            }),

            'profile_image': forms.ClearableFileInput(attrs={
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
        proof = self.cleaned_data.get(
            'proof_document'
        )

        if not proof:
            raise forms.ValidationError(
                'Please upload your proof document.'
            )

        allowed_extensions = (
            '.pdf',
            '.jpg',
            '.jpeg',
            '.png',
        )

        file_name = proof.name.lower()

        if not file_name.endswith(
            allowed_extensions
        ):
            raise forms.ValidationError(
                'Proof must be a PDF, JPG, JPEG or PNG file.'
            )

        # Maximum 5 MB
        if proof.size > 5 * 1024 * 1024:
            raise forms.ValidationError(
                'Proof document must be less than 5 MB.'
            )

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