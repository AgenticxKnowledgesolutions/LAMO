import os
from django.core.management.base import BaseCommand
from django.db import transaction
from lamoapp.models import CustomUser, WorkerProfile, Service


class Command(BaseCommand):
    help = "Seed verified workers across Kerala districts to demonstrate location-filtered services."

    SAMPLE_WORKERS = [
        # Kollam
        {
            "username": "suresh_plumber",
            "first_name": "Suresh",
            "last_name": "Kumar",
            "phone": "9847112233",
            "district": "Kollam",
            "service_id": 1,  # Plumbing Services
            "experience": 6,
            "pricing": 350.00,
            "current_place": "Chinnakada, Kollam",
        },
        {
            "username": "anitha_cleaner",
            "first_name": "Anitha",
            "last_name": "Raj",
            "phone": "9847223344",
            "district": "Kollam",
            "service_id": 3,  # Home Cleaning
            "experience": 4,
            "pricing": 599.00,
            "current_place": "Asramam, Kollam",
        },
        {
            "username": "rahul_ac",
            "first_name": "Rahul",
            "last_name": "Varghese",
            "phone": "9847334455",
            "district": "Kollam",
            "service_id": 6,  # AC Repair & Servicing
            "experience": 7,
            "pricing": 499.00,
            "current_place": "Kavanad, Kollam",
        },
        {
            "username": "vijayan_coconut",
            "first_name": "Vijayan",
            "last_name": "K",
            "phone": "9847445566",
            "district": "Kollam",
            "service_id": 18,  # Coconut Tree Climbing
            "experience": 12,
            "pricing": 350.00,
            "current_place": "Mayyanad, Kollam",
        },
        {
            "username": "thomas_carpenter",
            "first_name": "Thomas",
            "last_name": "Mathew",
            "phone": "9847556677",
            "district": "Kollam",
            "service_id": 4,  # Carpentry & Woodwork
            "experience": 8,
            "pricing": 450.00,
            "current_place": "Kundara, Kollam",
        },

        # Thiruvananthapuram
        {
            "username": "biju_electrician",
            "first_name": "Biju",
            "last_name": "S",
            "phone": "9847667788",
            "district": "Thiruvananthapuram",
            "service_id": 2,  # Electrician
            "experience": 9,
            "pricing": 350.00,
            "current_place": "Pattom, Thiruvananthapuram",
        },
        {
            "username": "arun_cctv",
            "first_name": "Arun",
            "last_name": "Nair",
            "phone": "9847778899",
            "district": "Thiruvananthapuram",
            "service_id": 9,  # CCTV & Smart Security
            "experience": 5,
            "pricing": 550.00,
            "current_place": "Kazhakkoottam, Thiruvananthapuram",
        },
        {
            "username": "vinod_solar",
            "first_name": "Vinod",
            "last_name": "Prasad",
            "phone": "9847889900",
            "district": "Thiruvananthapuram",
            "service_id": 13,  # Solar Installation
            "experience": 6,
            "pricing": 899.00,
            "current_place": "Pettah, Thiruvananthapuram",
        },
        {
            "username": "sasi_gardener",
            "first_name": "Sasi",
            "last_name": "Kumar",
            "phone": "9847990011",
            "district": "Thiruvananthapuram",
            "service_id": 12,  # Gardening
            "experience": 10,
            "pricing": 450.00,
            "current_place": "Vattiyoorkavu, Thiruvananthapuram",
        },

        # Ernakulam
        {
            "username": "george_ac",
            "first_name": "George",
            "last_name": "Joseph",
            "phone": "9846112233",
            "district": "Ernakulam",
            "service_id": 6,  # AC Repair & Servicing
            "experience": 8,
            "pricing": 499.00,
            "current_place": "Kaloor, Ernakulam",
        },
        {
            "username": "faisal_appliance",
            "first_name": "Faisal",
            "last_name": "K",
            "phone": "9846223344",
            "district": "Ernakulam",
            "service_id": 8,  # Appliance Repair
            "experience": 5,
            "pricing": 399.00,
            "current_place": "Edappally, Ernakulam",
        },
        {
            "username": "renjith_sofa",
            "first_name": "Renjith",
            "last_name": "R",
            "phone": "9846334455",
            "district": "Ernakulam",
            "service_id": 26,  # Sofa Shampooing
            "experience": 4,
            "pricing": 700.00,
            "current_place": "Kakkanad, Ernakulam",
        },
        {
            "username": "jithin_car",
            "first_name": "Jithin",
            "last_name": "Paul",
            "phone": "9846445566",
            "district": "Ernakulam",
            "service_id": 17,  # Vehicle Detailing
            "experience": 6,
            "pricing": 399.00,
            "current_place": "Palarivattom, Ernakulam",
        },

        # Thrissur
        {
            "username": "pradeep_painter",
            "first_name": "Pradeep",
            "last_name": "Menon",
            "phone": "9846556677",
            "district": "Thrissur",
            "service_id": 5,  # Painting & Whitewashing
            "experience": 11,
            "pricing": 700.00,
            "current_place": "Round West, Thrissur",
        },
        {
            "username": "balan_mason",
            "first_name": "Balan",
            "last_name": "C",
            "phone": "9846667788",
            "district": "Thrissur",
            "service_id": 10,  # Masonry & Tile Work
            "experience": 15,
            "pricing": 800.00,
            "current_place": "Ayyanthole, Thrissur",
        },

        # Kozhikode
        {
            "username": "nabeel_chimney",
            "first_name": "Nabeel",
            "last_name": "K",
            "phone": "9846778899",
            "district": "Kozhikode",
            "service_id": 22,  # Kitchen Chimney Cleaning
            "experience": 5,
            "pricing": 650.00,
            "current_place": "Mavoor Road, Kozhikode",
        },
        {
            "username": "haris_inverter",
            "first_name": "Haris",
            "last_name": "P",
            "phone": "9846889900",
            "district": "Kozhikode",
            "service_id": 21,  # Inverter & Battery
            "experience": 7,
            "pricing": 399.00,
            "current_place": "Palayam, Kozhikode",
        },
    ]

    def handle(self, *args, **options):
        self.stdout.write("Seeding sample verified workers across Kerala districts...")
        created_count = 0
        updated_count = 0

        with transaction.atomic():
            for w in self.SAMPLE_WORKERS:
                try:
                    service = Service.objects.get(id=w["service_id"])
                except Service.DoesNotExist:
                    continue

                user, u_created = CustomUser.objects.get_or_create(
                    username=w["username"],
                    defaults={
                        "first_name": w["first_name"],
                        "last_name": w["last_name"],
                        "email": f"{w['username']}@lamo.in",
                        "role": "WORKER",
                        "is_active": True,
                    }
                )
                if u_created:
                    user.set_password("Lamo@123")
                    user.save()

                wp, wp_created = WorkerProfile.objects.update_or_create(
                    user=user,
                    defaults={
                        "phone": w["phone"],
                        "service": service,
                        "experience_years": w["experience"],
                        "district": w["district"],
                        "availability": "Full Time",
                        "work_status": "AVAILABLE",
                        "pricing_type": "hour",
                        "pricing_amount": w["pricing"],
                        "is_verified": True,
                        "current_place": w["current_place"],
                        "proof_type": "aadhaar",
                    }
                )

                if wp_created:
                    created_count += 1
                else:
                    updated_count += 1

        self.stdout.write(
            f"Successfully seeded workers: {created_count} created, {updated_count} updated."
        )
