from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from lamoapp.models import Service


SERVICES_DATA = [
    # -----------------------------------------------------------------
    # EXISTING 17 SERVICES (Updated with standard names & base prices)
    # -----------------------------------------------------------------
    {
        'name': 'Plumbing Services',
        'legacy_name': 'plumber',
        'description': 'Professional plumbing services for homes and offices, including tap repairs, pipe leakage fixing, bathroom fittings, drain cleaning, and water supply maintenance.',
        'price': Decimal('350.00'),
        'is_active': True,
        'is_featured': True,
        'image': 'services/plumber.png',
    },
    {
        'name': 'Electrician',
        'description': 'Safe and reliable electrical services, including wiring, switch and socket repairs, fan installation, lighting setup, and electrical fault troubleshooting.',
        'price': Decimal('350.00'),
        'is_active': True,
        'is_featured': True,
        'image': 'services/electrician.png',
    },
    {
        'name': 'Home Cleaning',
        'description': 'Thorough home cleaning services covering bedrooms, living rooms, kitchens, bathrooms, dust removal, floor cleaning, and general household maintenance.',
        'price': Decimal('599.00'),
        'is_active': True,
        'is_featured': True,
        'image': 'services/cleaning.png',
    },
    {
        'name': 'Carpentry & Woodwork',
        'description': 'Custom furniture repair, doors, windows, locks, wooden fixtures, and modular woodwork.',
        'price': Decimal('450.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/carpentry.webp',
    },
    {
        'name': 'Painting & Whitewashing',
        'description': 'Interior and exterior wall painting, waterproof coating, primer application, and texture finishes.',
        'price': Decimal('700.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/painting.webp',
    },
    {
        'name': 'AC Repair & Servicing',
        'description': 'Air conditioner filter cleanup, gas refilling, cooling diagnostics, and installation/uninstallation.',
        'price': Decimal('499.00'),
        'is_active': True,
        'is_featured': True,
        'image': 'services/ac-repair.webp',
    },
    {
        'name': 'Pest Control',
        'description': 'Certified termite, cockroach, bed bug, and mosquito control using safe and eco-friendly solutions.',
        'price': Decimal('699.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/pest-control.webp',
    },
    {
        'name': 'Appliance Repair',
        'description': 'Troubleshooting and repair for washing machines, refrigerators, microwave ovens, and induction stoves.',
        'price': Decimal('399.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/appliance.webp',
    },
    {
        'name': 'CCTV & Smart Security',
        'description': 'Home security camera setup, DVR/NVR configuration, biometric access locks, and video doorbells.',
        'price': Decimal('550.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/cctv.webp',
    },
    {
        'name': 'Masonry & Tile Work',
        'description': 'Brickwork, floor and bathroom tiling, granite counter setting, and wall crack repair.',
        'price': Decimal('600.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/masonry.webp',
    },
    {
        'name': 'Roofing & Waterproofing',
        'description': 'Truss roofing, rain water gutter installation, terrace waterproofing, and leak proofing.',
        'price': Decimal('750.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/roofing.webp',
    },
    {
        'name': 'Gardening & Landscaping',
        'description': 'Lawn mowing, tree pruning, organic vegetable patch setup, and overall landscape maintenance.',
        'price': Decimal('450.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/gardening.webp',
    },
    {
        'name': 'Solar Installation & Repair',
        'description': 'Rooftop on-grid and off-grid solar panel setup, inverter battery wiring, and panel cleaning.',
        'price': Decimal('899.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/solar.webp',
    },
    {
        'name': 'Welding & Fabrication',
        'description': 'Metal gate repairs, window grill welding, safety railings, and custom metal fabrication.',
        'price': Decimal('500.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/welding.webp',
    },
    {
        'name': 'Water Tank & Sump Cleaning',
        'description': 'Mechanical scrubbing, high-pressure washing, UV antibacterial sanitization of overhead and underground tanks.',
        'price': Decimal('650.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/tank.webp',
    },
    {
        'name': 'Packers & Movers',
        'description': 'Safe household packing, furniture wrapping, loading/unloading, and local shifting assistance.',
        'price': Decimal('1500.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/packers.webp',
    },
    {
        'name': 'Vehicle Detailing & Wash',
        'description': 'Doorstep exterior foam wash, interior vacuuming, dashboard polishing, and sanitization for cars and bikes.',
        'price': Decimal('399.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/wash.webp',
    },

    # -----------------------------------------------------------------
    # 10 NEW PROPOSED SERVICES
    # -----------------------------------------------------------------
    {
        'name': 'Coconut Tree Climbing & Harvesting',
        'description': 'Professional mechanized and traditional coconut tree climbing, crown cleaning, nut harvesting, and pest inspection across compound properties.',
        'price': Decimal('350.00'),
        'is_active': True,
        'is_featured': True,
        'image': 'services/coconut.webp',
    },
    {
        'name': 'Well Cleaning & Water Chlorination',
        'description': 'Traditional open well desilting, sediment mud removal, wall scrubbing, debris extraction, and safe water chlorination/sanitization.',
        'price': Decimal('1200.00'),
        'is_active': True,
        'is_featured': True,
        'image': 'services/well-cleaning.webp',
    },
    {
        'name': 'Elderly Care & Home Nursing Assistant',
        'description': 'Compassionate home bedside assistance, patient mobility support, vital checks, medication reminders, and elderly companion care.',
        'price': Decimal('800.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/elder-care.webp',
    },
    {
        'name': 'Inverter & Battery Servicing',
        'description': 'Home UPS and inverter troubleshooting, battery distilled water top-up, acid gravity checks, terminal cleaning, and wiring diagnostics.',
        'price': Decimal('399.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/inverter.webp',
    },
    {
        'name': 'Kitchen Chimney & Exhaust Cleaning',
        'description': 'Complete kitchen chimney degreasing, baffle filter caustic wash, blower rotor cleanup, and heavy oil residue removal.',
        'price': Decimal('650.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/chimney.webp',
    },
    {
        'name': 'Tile, Marble & Granite Polishing',
        'description': 'High-speed rotary machine scrubbing, diamond pad scratch polishing, tile joint re-grouting, and protective mirror sealant coating.',
        'price': Decimal('1500.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/tile-polishing.webp',
    },
    {
        'name': 'Septic Tank & Waste Evacuation',
        'description': 'Heavy-duty vacuum suction tanker evacuation, underground septic tank desludging, emergency blockage clearing, and eco disposal.',
        'price': Decimal('2500.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/septic-tank.webp',
    },
    {
        'name': 'Grass Cutting & Bush Clearing',
        'description': 'Motorized brush cutter trimming, wild thorn bush clearance, overgrown lawn mowing, and monsoon yard sanitization.',
        'price': Decimal('500.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/grass-cutting.webp',
    },
    {
        'name': 'Sofa, Carpet & Mattress Shampooing',
        'description': 'Deep injection-extraction shampooing for fabric sofas, recliner armchairs, bedroom mattresses, and heavy wool carpets.',
        'price': Decimal('799.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/sofa-cleaning.webp',
    },
    {
        'name': 'Smart TV & Home Electronics Repair',
        'description': 'Component-level diagnostics and repair for LED/Smart TVs, soundbars, microwave ovens, induction cooktops, and home electronics.',
        'price': Decimal('450.00'),
        'is_active': True,
        'is_featured': False,
        'image': 'services/tv-repair.webp',
    },
]


class Command(BaseCommand):
    help = 'Seeds, updates, and expands LAMO services in the MySQL database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without committing them to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{'[DRY RUN] ' if dry_run else ''}Processing LAMO services..."
        ))

        created_count = 0
        updated_count = 0

        with transaction.atomic():
            for item in SERVICES_DATA:
                name = item['name']
                legacy_name = item.get('legacy_name')

                service = None
                if legacy_name:
                    service = Service.objects.filter(name=legacy_name).first()

                if not service:
                    service = Service.objects.filter(name=name).first()

                if service:
                    changed = False
                    if service.name != name:
                        self.stdout.write(f"Renaming '{service.name}' -> '{name}'")
                        service.name = name
                        changed = True

                    if service.description != item['description']:
                        service.description = item['description']
                        changed = True

                    if service.price is None and item['price'] is not None:
                        service.price = item['price']
                        changed = True

                    if item.get('is_featured') is not None and service.is_featured != item['is_featured']:
                        service.is_featured = item['is_featured']
                        changed = True

                    if not service.image and item.get('image'):
                        service.image = item['image']
                        changed = True

                    if changed:
                        if not dry_run:
                            service.save()
                        updated_count += 1
                        self.stdout.write(self.style.SUCCESS(f"  [UPDATED] {name}"))
                    else:
                        self.stdout.write(f"  [UNCHANGED] {name}")
                else:
                    if not dry_run:
                        Service.objects.create(
                            name=name,
                            description=item['description'],
                            price=item['price'],
                            is_active=item.get('is_active', True),
                            is_featured=item.get('is_featured', False),
                            image=item.get('image', ''),
                        )
                    created_count += 1
                    self.stdout.write(self.style.SUCCESS(f"  [CREATED] {name} (Rs. {item['price']})"))

            if dry_run:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING(
                    f"\nDry run complete: {created_count} would be created, {updated_count} would be updated."
                ))
            else:
                total = Service.objects.count()
                self.stdout.write(self.style.SUCCESS(
                    f"\nDone! Created: {created_count}, Updated: {updated_count}. Total services in DB: {total}."
                ))
