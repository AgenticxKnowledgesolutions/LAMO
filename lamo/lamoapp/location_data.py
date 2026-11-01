"""Comprehensive locality and town coordinates for all 14 Kerala districts.

Provides instant coordinate lookup, locality filtering, popular places,
and GPS-based nearest district/locality resolution.
"""

import math

# All 14 Kerala districts and their prominent localities with coordinates (lat, lng)
KERALA_DISTRICT_LOCALITIES = {
    'Thiruvananthapuram': {
        'Thiruvananthapuram City': (8.5241, 76.9366),
        'Kazhakkoottam': (8.5686, 76.8731),
        'Technopark': (8.5581, 76.8812),
        'Kovalam': (8.4004, 76.9787),
        'Neyyattinkara': (8.4034, 77.0863),
        'Attingal': (8.6968, 76.8142),
        'Varkala': (8.7379, 76.7163),
        'Nedumangad': (8.6019, 77.0006),
        'Kattakkada': (8.5074, 77.0805),
        'Parassala': (8.3431, 77.1517),
        'Vizhinjam': (8.3789, 77.0005),
        'Poojappura': (8.4947, 76.9697),
        'Vellayambalam': (8.5144, 76.9582),
        'Kowdiar': (8.5244, 76.9634),
        'Pattom': (8.5238, 76.9398),
        'Kesavadasapuram': (8.5366, 76.9362),
        'Sreekaryam': (8.5492, 76.9172),
        'Ulloor': (8.5372, 76.9248),
        'Peroorkada': (8.5342, 76.9744),
        'Sasthamangalam': (8.5089, 76.9702),
        'Medical College': (8.5233, 76.9272),
        'Palayam': (8.5015, 76.9515),
        'Thampanoor': (8.4891, 76.9528),
        'East Fort': (8.4831, 76.9458),
        'Karamana': (8.4812, 76.9631),
        'Nemom': (8.4552, 77.0012),
        'Balaramapuram': (8.4239, 77.0458),
        'Pothencode': (8.6052, 76.8972),
        'Kaniyapuram': (8.5912, 76.8532),
        'Mangalapuram': (8.6214, 76.8375),
        'Venjaramoodu': (8.6811, 76.9124),
        'Kilimanoor': (8.7667, 76.8794),
        'Vithura': (8.6815, 77.1082),
        'Aryanad': (8.5962, 77.1521),
        'Aruvikkara': (8.5684, 77.0195),
        'Chirayinkeezhu': (8.6542, 76.7901),
        'Vembayam': (8.6432, 76.9421),
        'Malayinkeezhu': (8.4851, 77.0421),
        'Vilappil': (8.5124, 77.0321),
        'Peyad': (8.5021, 77.0142),
    },
    'Kollam': {
        'Kollam City': (8.8932, 76.6141),
        'Karunagappally': (9.0544, 76.5369),
        'Kottarakkara': (9.0003, 76.7726),
        'Punalur': (9.0163, 76.9273),
        'Paravur': (8.8116, 76.6698),
        'Sasthamkotta': (9.0436, 76.6346),
        'Chavara': (8.9989, 76.5398),
        'Kundara': (8.9612, 76.6834),
        'Anchal': (8.9408, 76.9124),
        'Pathanapuram': (9.0911, 76.8576),
        'Chathannoor': (8.8571, 76.7198),
        'Kadakkal': (8.8251, 76.9248),
        'Ochira': (9.1245, 76.5052),
        'Mayyanad': (8.8312, 76.6421),
        'Eravipuram': (8.8562, 76.6148),
        'Kilikollur': (8.8982, 76.6341),
        'Tangasseri': (8.8872, 76.5682),
        'Thirumullavaram': (8.9042, 76.5621),
        'Sakthikulangara': (8.9324, 76.5412),
        'Neendakara': (8.9482, 76.5381),
        'Ayur': (8.8912, 76.8541),
        'Kulathupuzha': (8.9021, 77.0621),
        'Aryankavu': (8.9812, 77.1421),
        'Chadayamangalam': (8.8682, 76.8712),
        'Pooyappally': (8.9321, 76.7921),
        'Veliyam': (8.9712, 76.7412),
        'Kottiyam': (8.8612, 76.6691),
        'Kannanalloor': (8.8821, 76.6621),
        'Parippally': (8.8124, 76.7612),
        'Odanavattom': (8.9812, 76.8041),
    },
    'Pathanamthitta': {
        'Pathanamthitta Town': (9.2648, 76.7874),
        'Thiruvalla': (9.3835, 76.5741),
        'Adoor': (9.1530, 76.7356),
        'Ranni': (9.3812, 76.8124),
        'Konni': (9.2312, 76.8421),
        'Kozhencherry': (9.3412, 76.7021),
        'Mallappally': (9.4512, 76.6421),
        'Pandalam': (9.2312, 76.6812),
        'Aranmula': (9.3312, 76.6821),
        'Kumbanad': (9.3621, 76.6541),
        'Maramon': (9.3452, 76.6712),
        'Vennikulam': (9.4012, 76.6421),
        'Parumala': (9.3512, 76.5512),
        'Pulikeezhu': (9.3612, 76.5412),
        'Elavumthitta': (9.2512, 76.7212),
        'Kodumon': (9.1912, 76.7712),
        'Enathu': (9.1021, 76.7412),
        'Chittar': (9.3212, 76.9212),
        'Seethathode': (9.3312, 77.0121),
        'Vadasserikkara': (9.3312, 76.8212),
        'Perunad': (9.3612, 76.8821),
        'Kalanjoor': (9.1412, 76.8621),
        'Malayalapuzha': (9.2912, 76.8312),
        'Mylapra': (9.2812, 76.7921),
        'Thumpamon': (9.2112, 76.6921),
    },
    'Alappuzha': {
        'Alappuzha Town': (9.4981, 76.3388),
        'Cherthala': (9.6844, 76.3328),
        'Kayamkulam': (9.1724, 76.5012),
        'Mavelikkara': (9.2435, 76.5482),
        'Chengannur': (9.3174, 76.6112),
        'Haripad': (9.2812, 76.4521),
        'Ambalappuzha': (9.3812, 76.3541),
        'Aroor': (9.8712, 76.3021),
        'Kuthiathode': (9.7912, 76.3212),
        'Muhamma': (9.6012, 76.3621),
        'Mannar': (9.3124, 76.5241),
        'Vallikunnam': (9.1421, 76.5421),
        'Nooranad': (9.2012, 76.6212),
        'Chettikulangara': (9.2312, 76.5212),
        'Edathua': (9.3621, 76.4621),
        'Champakulam': (9.4121, 76.4121),
        'Pulinkunnu': (9.4421, 76.4321),
        'Ramankary': (9.4012, 76.4821),
        'Kuttanad': (9.4312, 76.4521),
        'Mararikulam': (9.6012, 76.3121),
        'Punnapra': (9.4412, 76.3512),
        'Kalavoor': (9.5512, 76.3321),
        'Komalapuram': (9.5312, 76.3312),
        'Thanneermukkom': (9.6912, 76.3921),
        'Veeyapuram': (9.3212, 76.4821),
    },
    'Kottayam': {
        'Kottayam Town': (9.5916, 76.5224),
        'Changanassery': (9.4447, 76.5369),
        'Pala': (9.7112, 76.6834),
        'Kanjirappally': (9.5582, 76.7824),
        'Vaikom': (9.7482, 76.3962),
        'Ettumanoor': (9.6682, 76.5621),
        'Erattupetta': (9.6912, 76.7821),
        'Pampady': (9.5712, 76.6521),
        'Puthuppally': (9.5612, 76.5721),
        'Manarcad': (9.6012, 76.5821),
        'Ayarkunnam': (9.6412, 76.6121),
        'Athirampuzha': (9.6542, 76.5312),
        'Gandhinagar': (9.6212, 76.5212),
        'Kumarakom': (9.6175, 76.4302),
        'Kaduthuruthy': (9.7612, 76.4821),
        'Kuravilangad': (9.7512, 76.5621),
        'Ramapuram': (9.7812, 76.6421),
        'Mundakayam': (9.5312, 76.8821),
        'Ponkunnam': (9.5712, 76.7521),
        'Teekoy': (9.7212, 76.8321),
        'Thalayolaparambu': (9.7912, 76.4421),
        'Chingavanam': (9.5212, 76.5321),
        'Panachikkad': (9.5312, 76.5621),
        'Vazhoor': (9.5612, 76.7212),
        'Melukavu': (9.7512, 76.7521),
    },
    'Idukki': {
        'Thodupuzha': (9.8959, 76.7184),
        'Kattappana': (9.7712, 77.1214),
        'Munnar': (10.0889, 77.0595),
        'Adimali': (10.0121, 76.9542),
        'Nedumkandam': (9.8312, 77.1621),
        'Kumily': (9.6082, 77.1654),
        'Peermade': (9.5712, 76.9921),
        'Vagamon': (9.6892, 76.9054),
        'Cheruthoni': (9.8512, 76.9721),
        'Painavu': (9.8452, 76.9682),
        'Murickassery': (9.8912, 77.0212),
        'Rajakkad': (10.0121, 77.1212),
        'Marayoor': (10.2712, 77.1612),
        'Devikulam': (10.0612, 77.1021),
        'Santhanpara': (9.9712, 77.2112),
        'Anakkara': (9.6712, 77.1521),
        'Vandiperiyar': (9.5712, 77.0821),
        'Elappara': (9.6412, 76.9721),
        'Karikode': (9.9012, 76.7321),
        'Muttom': (9.8412, 76.7421),
        'Alakode': (9.8812, 76.7921),
        'Karimannoor': (9.9412, 76.7821),
        'Vandanmedu': (9.7312, 77.1521),
        'Udumbanchola': (9.8912, 77.2012),
        'Kanjikuzhy': (9.9212, 76.9521),
    },
    'Ernakulam': {
        'Kochi / Cochin': (9.9312, 76.2673),
        'Marine Drive': (9.9782, 76.2758),
        'MG Road': (9.9672, 76.2821),
        'Ernakulam North': (9.9912, 76.2862),
        'Ernakulam South': (9.9642, 76.2912),
        'Kakkanad': (10.0159, 76.3419),
        'Infopark': (10.0098, 76.3638),
        'Edappally': (10.0261, 76.3084),
        'Palarivattom': (10.0052, 76.3082),
        'Kaloor': (9.9942, 76.2982),
        'Aluva': (10.1076, 76.3516),
        'Angamaly': (10.1960, 76.3860),
        'Perumbavoor': (10.1132, 76.4782),
        'Muvattupuzha': (9.9832, 76.5784),
        'Kothamangalam': (10.0612, 76.6241),
        'Tripunithura': (9.9492, 76.3458),
        'Fort Kochi': (9.9658, 76.2421),
        'Mattancherry': (9.9572, 76.2582),
        'Thoppumpady': (9.9342, 76.2642),
        'Kalamassery': (10.0542, 76.3182),
        'Vyttila': (9.9672, 76.3212),
        'Maradu': (9.9362, 76.3312),
        'Kundannoor': (9.9282, 76.3192),
        'Cherai': (10.1412, 76.1792),
        'North Paravur': (10.1482, 76.2312),
        'Eloor': (10.0782, 76.3012),
        'Thrikkakara': (10.0352, 76.3352),
        'Piravom': (9.8672, 76.4921),
        'Kolenchery': (9.9762, 76.4712),
        'Vazhakkala': (10.0182, 76.3242),
        'Panampilly Nagar': (9.9612, 76.2982),
        'Ravipuram': (9.9562, 76.2912),
        'Thevara': (9.9382, 76.2972),
        'Kadavanthra': (9.9682, 76.3042),
        'Willingdon Island': (9.9521, 76.2712),
        'Varapuzha': (10.0812, 76.2712),
        'Kizhakkambalam': (10.0412, 76.4112),
        'Koothattukulam': (9.8612, 76.5821),
        'Kuruppampady': (10.1212, 76.5212),
        'Puthencruz': (9.9812, 76.4212),
    },
    'Thrissur': {
        'Thrissur City': (10.5276, 76.2144),
        'Round South': (10.5232, 76.2141),
        'Round North': (10.5312, 76.2148),
        'Swaraj Round': (10.5252, 76.2144),
        'Guruvayur': (10.5947, 76.0407),
        'Kunnamkulam': (10.6512, 76.0712),
        'Chalakudy': (10.3070, 76.3333),
        'Kodungallur': (10.2272, 76.1962),
        'Irinjalakuda': (10.3442, 76.2084),
        'Wadakkanchery': (10.6612, 76.2412),
        'Chavakkad': (10.5812, 76.0212),
        'Ollur': (10.4712, 76.2412),
        'Mannuthy': (10.5342, 76.2682),
        'Punkunnam': (10.5412, 76.1982),
        'Ayyanthole': (10.5282, 76.1842),
        'Ramavarmapuram': (10.5512, 76.2312),
        'Viyyur': (10.5562, 76.2182),
        'Kuriachira': (10.4982, 76.2212),
        'Cherpu': (10.4512, 76.2012),
        'Triprayar': (10.4182, 76.1182),
        'Amballur': (10.4312, 76.2812),
        'Mala': (10.2212, 76.2892),
        'Puthukkad': (10.4212, 76.2712),
        'Koratty': (10.2612, 76.3512),
        'Pazhayannur': (10.6812, 76.4312),
        'Chelakkara': (10.7012, 76.3412),
        'Athirappilly': (10.3012, 76.5412),
        'Kechery': (10.6012, 76.1312),
        'Peramangalam': (10.5712, 76.1512),
        'Anthikad': (10.4512, 76.1412),
    },
    'Palakkad': {
        'Palakkad Town': (10.7867, 76.6548),
        'Ottapalam': (10.7712, 76.3812),
        'Shoranur': (10.7612, 76.2812),
        'Chittur': (10.7012, 76.7512),
        'Thathamangalam': (10.6912, 76.7312),
        'Mannarkkad': (10.9912, 76.4612),
        'Pattambi': (10.8112, 76.1812),
        'Alathur': (10.6412, 76.5412),
        'Cherpulassery': (10.8712, 76.3112),
        'Vadakkencherry': (10.5912, 76.5012),
        'Kollengode': (10.6212, 76.7012),
        'Nemmara': (10.5912, 76.6012),
        'Kuzhalmannam': (10.7112, 76.5912),
        'Parli': (10.7912, 76.5512),
        'Kongad': (10.8912, 76.5212),
        'Malampuzha': (10.8312, 76.6812),
        'Walayar': (10.8412, 76.8412),
        'Kanjikode': (10.8012, 76.7412),
        'Kozhinjampara': (10.7412, 76.8112),
        'Koduvayur': (10.6812, 76.6512),
        'Thrithala': (10.8012, 76.1312),
        'Koppam': (10.8612, 76.2112),
        'Agali': (11.0812, 76.6512),
        'Attappadi': (11.0512, 76.6212),
        'Mundur': (10.8412, 76.5712),
    },
    'Malappuram': {
        'Malappuram Town': (11.0732, 76.1412),
        'Manjeri': (11.1212, 76.1212),
        'Perinthalmanna': (10.9762, 76.2254),
        'Tirur': (10.9162, 75.9234),
        'Ponnani': (10.7712, 75.9248),
        'Kottakkal': (11.0012, 75.9982),
        'Nilambur': (11.2782, 76.2272),
        'Kondotty': (11.1482, 75.9612),
        'Edappal': (10.8212, 76.0012),
        'Parappanangadi': (11.0512, 75.8612),
        'Tanur': (10.9712, 75.8712),
        'Valanchery': (10.8912, 76.0712),
        'Changaramkulam': (10.7812, 76.0312),
        'Kuttippuram': (10.8512, 76.0312),
        'Wandoor': (11.1912, 76.2412),
        'Pandikkad': (11.1212, 76.2712),
        'Melattur': (11.0512, 76.2912),
        'Areekode': (11.2312, 76.0512),
        'Mongam': (11.1812, 76.0012),
        'Morayur': (11.1612, 75.9812),
        'Vengara': (11.0512, 75.9612),
        'Chelari': (11.0912, 75.9012),
        'Kolathur': (10.9612, 76.1312),
        'Karipur': (11.1362, 75.9552),
        'Mankada': (11.0212, 76.1812),
    },
    'Kozhikode': {
        'Kozhikode City': (11.2588, 75.7804),
        'Mananchira': (11.2524, 75.7794),
        'Mavoor Road': (11.2612, 75.7942),
        'SM Street': (11.2512, 75.7782),
        'Palayam': (11.2482, 75.7832),
        'Calicut Beach': (11.2612, 75.7682),
        'Nadakkavu': (11.2712, 75.7782),
        'Westhill': (11.2912, 75.7642),
        'Elathur': (11.3412, 75.7412),
        'Feroke': (11.1812, 75.8312),
        'Beypore': (11.1612, 75.8012),
        'Ramanattukara': (11.1712, 75.8612),
        'Koyilandy': (11.4382, 75.6982),
        'Vatakara': (11.6082, 75.5912),
        'Thamarassery': (11.4112, 75.9312),
        'Koduvally': (11.3612, 75.9112),
        'Balussery': (11.4512, 75.8212),
        'Perambra': (11.5312, 75.7512),
        'Kunnamangalam': (11.3112, 75.8712),
        'Kakkodi': (11.3112, 75.7912),
        'Cheruvannur': (11.1912, 75.8212),
        'Pantheeramkavu': (11.2212, 75.8412),
        'Ulliyeri': (11.4612, 75.7812),
        'Thiruvambady': (11.3912, 76.0112),
        'Mukkam': (11.3212, 75.9812),
    },
    'Wayanad': {
        'Kalpetta': (11.6103, 76.0827),
        'Sultan Bathery': (11.6624, 76.2571),
        'Mananthavady': (11.8024, 76.0034),
        'Meppadi': (11.5512, 76.1312),
        'Vythiri': (11.5512, 76.0412),
        'Pozhuthana': (11.5812, 76.0212),
        'Ambalavayal': (11.6212, 76.2112),
        'Pulpally': (11.7912, 76.2812),
        'Panamaram': (11.7412, 76.0712),
        'Padinjarathara': (11.6712, 75.9712),
        'Meenangadi': (11.6612, 76.1712),
        'Muttil': (11.6312, 76.1112),
        'Kenichira': (11.7212, 76.1912),
        'Tharuvana': (11.7212, 76.0112),
        'Vellamunda': (11.7612, 75.9312),
        'Dwaraka': (11.7812, 76.0212),
        'Mullankolli': (11.8212, 76.2912),
        'Thariode': (11.6412, 75.9812),
        'Korome': (11.8312, 75.9612),
        'Rippon': (11.5312, 76.1612),
    },
    'Kannur': {
        'Kannur City': (11.8745, 75.3704),
        'Thana': (11.8712, 75.3782),
        'South Bazar': (11.8682, 75.3654),
        'Payyannur': (12.1012, 75.2012),
        'Thalassery': (11.7491, 75.4894),
        'Mattannur': (11.9212, 75.5712),
        'Iritty': (11.9812, 75.6612),
        'Taliparamba': (12.0412, 75.3512),
        'Koothuparamba': (11.8212, 75.5612),
        'Panoor': (11.7612, 75.5812),
        'Sreekandapuram': (12.0312, 75.5212),
        'Dharmadam': (11.7712, 75.4612),
        'Chakkarakkal': (11.8612, 75.4712),
        'Valapattanam': (11.9112, 75.3612),
        'Azhikode': (11.9212, 75.3112),
        'Pappinisseri': (11.9512, 75.3512),
        'Cherukunnu': (12.0012, 75.2912),
        'Pilathara': (12.0612, 75.2512),
        'Alakode': (12.1812, 75.4812),
        'Payyavoor': (12.0212, 75.6112),
        'Kelakam': (11.8912, 75.8312),
        'Peravoor': (11.9112, 75.7612),
        'Anjarakandy': (11.8512, 75.5112),
        'Kadachira': (11.8612, 75.4312),
        'Munderi': (11.8912, 75.4412),
    },
    'Kasaragod': {
        'Kasaragod Town': (12.4996, 75.0096),
        'Kanhangad': (12.3112, 75.0912),
        'Nileshwaram': (12.2512, 75.1312),
        'Uppala': (12.6812, 74.9012),
        'Manjeshwaram': (12.7112, 74.8812),
        'Kumbla': (12.5812, 74.9412),
        'Cheruvathur': (12.2112, 75.1612),
        'Bekal': (12.3912, 75.0312),
        'Trikaripur': (12.1512, 75.1612),
        'Badiadka': (12.5612, 75.0912),
        'Mulleria': (12.5512, 75.1812),
        'Mogral Puthur': (12.5312, 74.9612),
        'Seethangoli': (12.5812, 75.0212),
        'Vidyanagar': (12.5012, 75.0012),
        'Nullippady': (12.5112, 74.9912),
        'Kasaragod Beach': (12.4912, 74.9812),
        'Bovikanam': (12.5112, 75.1012),
        'Chengala': (12.5212, 75.0412),
        'Bandadka': (12.4812, 75.2912),
        'Panathur': (12.4712, 75.3612),
    },
}

# Pre-computed popular localities per district (for quick one-tap selection pills)
POPULAR_LOCALITIES = {
    'Thiruvananthapuram': ['Technopark', 'Kazhakkoottam', 'Pattom', 'Kowdiar', 'Attingal', 'Varkala', 'Neyyattinkara'],
    'Kollam': ['Karunagappally', 'Kottarakkara', 'Chavara', 'Kundara', 'Punalur', 'Paravur', 'Kollam City'],
    'Pathanamthitta': ['Thiruvalla', 'Adoor', 'Ranni', 'Konni', 'Kozhencherry', 'Pandalam'],
    'Alappuzha': ['Cherthala', 'Kayamkulam', 'Mavelikkara', 'Chengannur', 'Haripad', 'Alappuzha Town'],
    'Kottayam': ['Changanassery', 'Pala', 'Ettumanoor', 'Kanjirappally', 'Vaikom', 'Kottayam Town'],
    'Idukki': ['Thodupuzha', 'Kattappana', 'Munnar', 'Adimali', 'Kumily', 'Nedumkandam'],
    'Ernakulam': ['Kakkanad', 'Edappally', 'Kaloor', 'Aluva', 'Palarivattom', 'Tripunithura', 'Marine Drive'],
    'Thrissur': ['Thrissur City', 'Guruvayur', 'Chalakudy', 'Kunnamkulam', 'Kodungallur', 'Irinjalakuda'],
    'Palakkad': ['Palakkad Town', 'Ottapalam', 'Shoranur', 'Mannarkkad', 'Pattambi', 'Alathur'],
    'Malappuram': ['Manjeri', 'Perinthalmanna', 'Tirur', 'Kottakkal', 'Ponnani', 'Nilambur'],
    'Kozhikode': ['Kozhikode City', 'Mavoor Road', 'Nadakkavu', 'Vatakara', 'Koyilandy', 'Thamarassery'],
    'Wayanad': ['Kalpetta', 'Sultan Bathery', 'Mananthavady', 'Meppadi', 'Vythiri'],
    'Kannur': ['Kannur City', 'Thalassery', 'Payyannur', 'Taliparamba', 'Mattannur', 'Iritty'],
    'Kasaragod': ['Kasaragod Town', 'Kanhangad', 'Nileshwaram', 'Uppala', 'Bekal', 'Cheruvathur'],
}


def get_all_districts():
    """Return sorted list of district names."""
    return list(KERALA_DISTRICT_LOCALITIES.keys())


def get_localities_for_district(district_name):
    """Return list of locality names for a district."""
    if not district_name:
        return []
    for dist, localities in KERALA_DISTRICT_LOCALITIES.items():
        if dist.lower() == district_name.strip().lower():
            return list(localities.keys())
    return []


def get_popular_localities(district_name):
    """Return top popular locality names for 1-tap chip display."""
    if not district_name:
        return []
    for dist, pops in POPULAR_LOCALITIES.items():
        if dist.lower() == district_name.strip().lower():
            return pops
    localities = get_localities_for_district(district_name)
    return localities[:6]


def get_locality_coordinates(district_name, locality_name):
    """Return (lat, lng) for a specific district and locality, or (None, None)."""
    if not locality_name:
        return None, None

    loc_clean = locality_name.strip().lower()

    # If district given, check that district first
    if district_name:
        for dist, localities in KERALA_DISTRICT_LOCALITIES.items():
            if dist.lower() == district_name.strip().lower():
                for name, coords in localities.items():
                    if name.lower() == loc_clean or loc_clean in name.lower() or name.lower() in loc_clean:
                        return coords

    # Otherwise scan all districts for a match
    for dist, localities in KERALA_DISTRICT_LOCALITIES.items():
        for name, coords in localities.items():
            if name.lower() == loc_clean:
                return coords
            if loc_clean in name.lower() or name.lower() in loc_clean:
                return coords

    return None, None


def _haversine(lat1, lon1, lat2, lon2):
    """Haversine distance in km."""
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_km * c


def find_nearest_district_and_locality(lat, lon):
    """Given user GPS coordinates, find the closest Kerala district and locality."""
    try:
        user_lat = float(lat)
        user_lon = float(lon)
    except (TypeError, ValueError):
        return None, None, None

    best_dist = float('inf')
    best_district = None
    best_locality = None

    for district, localities in KERALA_DISTRICT_LOCALITIES.items():
        for locality, (loc_lat, loc_lon) in localities.items():
            d = _haversine(user_lat, user_lon, loc_lat, loc_lon)
            if d < best_dist:
                best_dist = d
                best_district = district
                best_locality = locality

    return best_district, best_locality, round(best_dist, 1)


def get_kerala_location_tree():
    """Return JSON-serializable dictionary with districts and full locality lists."""
    tree = {}
    for district, localities in KERALA_DISTRICT_LOCALITIES.items():
        tree[district] = {
            'popular': POPULAR_LOCALITIES.get(district, list(localities.keys())[:6]),
            'localities': [
                {
                    'name': name,
                    'lat': coords[0],
                    'lng': coords[1],
                }
                for name, coords in localities.items()
            ],
        }
    return tree
