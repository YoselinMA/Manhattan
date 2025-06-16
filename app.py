from flask import Flask, render_template, request, jsonify
import requests
import math

app = Flask(__name__)

# --- URLs de APIs externas ---
NOMINATIM_API_URL = "https://nominatim.openstreetmap.org/search"
OSRM_API_URL = "http://router.project-osrm.org/route/v1"
OVERPASS_API_URL = "http://overpass-api.de/api/interpreter"

# --- Parámetros para Simulación de Costos y Emisiones ---
CAR_EFFICIENCY_KML = 12.0
MOTO_EFFICIENCY_KML = 25.0
FUEL_PRICE_MXN = 23.50
TOLL_PRICE_PER_KM_MXN = 1.80
CAR_CARBON_G_PER_KM = 184.0
MOTO_CARBON_G_PER_KM = 98.0
FLIGHT_CARBON_G_PER_KM = 255.0  # Emisiones aproximadas por km de vuelo

# --- Velocidades promedio para cálculo de tiempos (km/h) ---
CAR_SPEED_URBAN = 50.0
CAR_SPEED_HIGHWAY = 80.0
MOTO_SPEED_FACTOR = 1.2  # 20% más rápido que el carro
BIKE_SPEED = 15.0
WALK_SPEED = 5.0

# --- Funciones Auxiliares ---

def haversine(lat1, lon1, lat2, lon2):
    """Calcula la distancia del círculo máximo entre dos puntos en la tierra (especificada en kilómetros)."""
    R = 6371  # Radio de la Tierra en km
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon / 2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c

def find_nearest_airport(lat, lon, max_distance_km=500):
    """Encuentra el aeropuerto más cercano a una latitud y longitud dadas usando la API de Overpass."""
    query = f"""
    [out:json][timeout:25];
    (
      node(around:{max_distance_km * 1000},{lat},{lon})[aeroway=aerodrome];
      way(around:{max_distance_km * 1000},{lat},{lon})[aeroway=aerodrome];
      relation(around:{max_distance_km * 1000},{lat},{lon})[aeroway=aerodrome];
    );
    out center;
    """
    try:
        response = requests.post(OVERPASS_API_URL, data=query, timeout=25)
        response.raise_for_status()
        data = response.json()
        
        if not data['elements']:
            return None
            
        elements = []
        for element in data['elements']:
            if 'tags' not in element: continue
            # Filtra aeropuertos con código IATA para asegurar que sean relevantes
            if 'iata' in element['tags']:
                if 'lat' in element and 'lon' in element:
                    el_lat, el_lon = element['lat'], element['lon']
                elif 'center' in element:
                    el_lat, el_lon = element['center']['lat'], element['center']['lon']
                else:
                    continue
                
                distance = haversine(lat, lon, el_lat, el_lon)
                name = element.get('tags', {}).get('name', 'Aeropuerto')
                iata = element.get('tags', {}).get('iata')
                elements.append({
                    'distance': distance,
                    'lat': el_lat,
                    'lon': el_lon,
                    'name': f"{name} ({iata})"
                })
        
        if elements:
            elements.sort(key=lambda x: x['distance'])
            return elements[0]
            
    except requests.RequestException as e:
        print(f"Error buscando aeropuertos: {e}")
    return None

def calculate_flight_details(start_coords, end_coords):
    """Calcula los detalles de un vuelo directo de punto a punto."""
    flight_dist_km = round(haversine(start_coords[0], start_coords[1], end_coords[0], end_coords[1]), 2)
    flight_duration_s = (flight_dist_km / 800) * 3600  # Estimación: velocidad de crucero 800 km/h
    airport_time_s = 2 * 3600  # Tiempo adicional estimado en aeropuertos (check-in, seguridad, etc.)
    
    return {
        'duration_s': flight_duration_s + airport_time_s,
        'distance_km': flight_dist_km,
        'carbon_g': flight_dist_km * FLIGHT_CARBON_G_PER_KM,
        'route_type': 'aerial'
    }

def calculate_route_time(distance_km, mode):
    """Calcula el tiempo estimado de viaje según el modo de transporte."""
    if mode == 'car':
        # Estimación: 70% urbano (más lento), 30% carretera (más rápido)
        urban_dist = distance_km * 0.7
        highway_dist = distance_km * 0.3
        return (urban_dist / CAR_SPEED_URBAN + highway_dist / CAR_SPEED_HIGHWAY) * 3600
    
    elif mode == 'motorcycle':
        # Motocicleta es 20% más rápida que el carro en promedio
        car_time = calculate_route_time(distance_km, 'car')
        return car_time / MOTO_SPEED_FACTOR
    
    elif mode == 'bike':
        return (distance_km / BIKE_SPEED) * 3600
    
    elif mode == 'walk':
        return (distance_km / WALK_SPEED) * 3600
    
    return 0

# --- Rutas de la Aplicación ---

@app.route('/')
def index():
    return render_template('mapa.html')

@app.route('/buscar_lugar')
def buscar_lugar():
    """Busca un lugar usando Nominatim API."""
    query = request.args.get('q')
    if not query:
        return jsonify({'status': 'error', 'message': 'El nombre del lugar es requerido'}), 400
    
    params = {'q': query, 'format': 'json', 'limit': 1}
    headers = {'User-Agent': 'MiGPSApp/1.0'}
    
    try:
        response = requests.get(NOMINATIM_API_URL, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data:
            return jsonify({'status': 'error', 'message': 'Lugar no encontrado'}), 404
        location = data[0]
        return jsonify({'lat': float(location['lat']), 'lon': float(location['lon'])})
    except requests.RequestException as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/calcular_ruta', methods=['POST'])
def calcular_ruta():
    """Calcula rutas terrestres y aéreas, manejando errores si la terrestre no está disponible."""
    data = request.json
    start_coords = data['start']
    end_coords = data['end']
    
    distance_direct = haversine(start_coords[0], start_coords[1], end_coords[0], end_coords[1])
    
    all_details = {}
    terrestrial_route_geom = None
    manhattan_path_geom = None
    multimodal_parts_geom = None
    airports_info = None

    # 1. Intentar calcular ruta terrestre
    try:
        terrestrial_result = calculate_terrestrial_route(start_coords, end_coords)
        terrestrial_data = terrestrial_result.get_json()
        if terrestrial_data['status'] == 'success':
            all_details.update(terrestrial_data['details'])
            terrestrial_route_geom = terrestrial_data.get('real_route')
            manhattan_path_geom = terrestrial_data.get('manhattan_path')
    except Exception as e:
        print(f"No se pudo calcular la ruta terrestre: {e}")

    # 2. Si la distancia es grande, considerar vuelos
    if distance_direct > 300:
        # Añadir opción de vuelo directo
        all_details['flight'] = calculate_flight_details(start_coords, end_coords)
        
        # Intentar calcular ruta multimodal (coche-vuelo-coche)
        try:
            multimodal_result = calculate_multimodal_route(start_coords, end_coords)
            multimodal_data = multimodal_result.get_json()
            if multimodal_data['status'] == 'success':
                all_details['multimodal_flight'] = multimodal_data['details']['flight']
                multimodal_parts_geom = multimodal_data.get('parts')
                airports_info = multimodal_data.get('airports')
        except Exception as e:
            print(f"No se pudo calcular la ruta multimodal: {e}")

    if not all_details:
        return jsonify({
            'status': 'error', 
            'message': 'No se pudo calcular ninguna ruta para los puntos seleccionados.'
        }), 500

    return jsonify({
        'status': 'success',
        'route_type': 'combined',
        'details': all_details,
        'terrestrial_route': terrestrial_route_geom,
        'manhattan_path': manhattan_path_geom,
        'multimodal_parts': multimodal_parts_geom,
        'airports': airports_info
    })

def calculate_terrestrial_route(start_coords, end_coords):
    """Calcula rutas para diferentes modos terrestres usando OSRM."""
    coords_str = f"{start_coords[1]},{start_coords[0]};{end_coords[1]},{end_coords[0]}"
    details = {}
    real_route_geom = None
    manhattan_path_geom = None

    # Obtener ruta principal para coche para tener una referencia de distancia
    try:
        main_route_req = requests.get(f"{OSRM_API_URL}/driving/{coords_str}?overview=full&geometries=geojson", timeout=15)
        main_route_req.raise_for_status()
        main_route_data = main_route_req.json()
        if 'routes' not in main_route_data or not main_route_data['routes']:
             raise Exception("No se encontró ruta en OSRM")

        car_route = main_route_data['routes'][0]
        distance_km = round(car_route['distance'] / 1000, 2)
        real_route_geom = [[p[1], p[0]] for p in car_route['geometry']['coordinates']]
        
        # Cálculo de Manhattan
        mp = real_route_geom[-2] if len(real_route_geom) > 1 else start_coords
        mc = [end_coords[0], mp[1]]
        manhattan_path_geom = [mp, mc, end_coords]

        # Detalles para coche y moto con tiempos calculados según el modo
        details['car'] = {
            'duration_s': calculate_route_time(distance_km, 'car'),
            'distance_km': distance_km,
            'tolls_mxn': distance_km * TOLL_PRICE_PER_KM_MXN,
            'fuel_mxn': (distance_km / CAR_EFFICIENCY_KML) * FUEL_PRICE_MXN,
            'carbon_g': distance_km * CAR_CARBON_G_PER_KM,
            'route_type': 'terrestrial'
        }
        details['motorcycle'] = {
            'duration_s': calculate_route_time(distance_km, 'motorcycle'),
            'distance_km': distance_km,
            'tolls_mxn': distance_km * TOLL_PRICE_PER_KM_MXN,
            'fuel_mxn': (distance_km / MOTO_EFFICIENCY_KML) * FUEL_PRICE_MXN,
            'carbon_g': distance_km * MOTO_CARBON_G_PER_KM,
            'route_type': 'terrestrial'
        }

    except requests.RequestException as e:
        raise Exception(f"Error inicial en OSRM (coche): {str(e)}")

    # Calcular rutas para bicicleta y a pie (solo si es viable)
    other_profiles = {'bike': 'cycling', 'walk': 'walking'}
    for mode, profile in other_profiles.items():
        # Limitar cálculo para distancias razonables
        max_dist = 200 if mode == 'bike' else 50
        if distance_km > max_dist:
            continue

        try:
            route_req = requests.get(f"{OSRM_API_URL}/{profile}/{coords_str}", timeout=15)
            if route_req.status_code == 200:
                route_data = route_req.json()['routes'][0]
                route_distance_km = round(route_data['distance'] / 1000, 2)
                details[mode] = {
                    'duration_s': calculate_route_time(route_distance_km, mode),
                    'distance_km': route_distance_km,
                    'tolls_mxn': 0, 'fuel_mxn': 0, 'carbon_g': 0,
                    'route_type': 'terrestrial'
                }
        except requests.RequestException:
            print(f"No se pudo obtener la ruta para el perfil: {profile}")

    return jsonify({
        'status': 'success',
        'real_route': real_route_geom,
        'manhattan_path': manhattan_path_geom,
        'details': details
    })

def calculate_multimodal_route(start_coords, end_coords):
    """Calcula una ruta multimodal combinando coche y vuelo con Manhattan cuando es necesario."""
    # Encontrar aeropuertos más cercanos
    start_airport = find_nearest_airport(start_coords[0], start_coords[1], max_distance_km=500)
    end_airport = find_nearest_airport(end_coords[0], end_coords[1], max_distance_km=500)
    
    if not start_airport or not end_airport or start_airport['name'] == end_airport['name']:
        raise Exception("No se encontraron aeropuertos adecuados para una ruta multimodal.")
    
    try:
        # 1. Ruta terrestre desde origen al aeropuerto de salida
        r1 = requests.get(f"{OSRM_API_URL}/driving/{start_coords[1]},{start_coords[0]};{start_airport['lon']},{start_airport['lat']}?overview=full&geometries=geojson", timeout=15).json()
        
        # Verificar si la ruta existe
        if 'routes' not in r1 or not r1['routes']:
            raise Exception("No se pudo calcular la ruta terrestre al aeropuerto")
            
        r1_route = r1['routes'][0]
        ground1_dist_km = round(r1_route['distance'] / 1000, 2)
        ground1_duration_s = r1_route['duration']
        r1_geom = [[p[1], p[0]] for p in r1_route['geometry']['coordinates']]

        # 2. Ruta desde aeropuerto de llegada al destino final
        airport_to_dest_dist = haversine(end_airport['lat'], end_airport['lon'], end_coords[0], end_coords[1])
        
        # Si está lejos (>50km), aplicar Manhattan para la primera parte
        if airport_to_dest_dist > 50:
            # Punto intermedio para ruta Manhattan (primero ajustar latitud, luego longitud)
            midpoint = [end_airport['lat'], end_coords[1]]  # Primero ir en línea recta en longitud
            
            # Dividir la ruta en dos segmentos
            # Segmento 1: Aeropuerto -> Punto intermedio (Manhattan)
            r2a = requests.get(f"{OSRM_API_URL}/driving/{end_airport['lon']},{end_airport['lat']};{midpoint[1]},{midpoint[0]}?overview=full&geometries=geojson", timeout=15).json()
            
            # Segmento 2: Punto intermedio -> Destino final
            r2b = requests.get(f"{OSRM_API_URL}/driving/{midpoint[1]},{midpoint[0]};{end_coords[1]},{end_coords[0]}?overview=full&geometries=geojson", timeout=15).json()
            
            if 'routes' not in r2a or not r2a['routes'] or 'routes' not in r2b or not r2b['routes']:
                raise Exception("No se pudo calcular la ruta terrestre desde el aeropuerto")
                
            r2a_route, r2b_route = r2a['routes'][0], r2b['routes'][0]
            ground2_dist_km = round((r2a_route['distance'] + r2b_route['distance']) / 1000, 2)
            ground2_duration_s = r2a_route['duration'] + r2b_route['duration']
            
            # Combinar geometrías para dibujar
            r2_geom = [[p[1], p[0]] for p in r2a_route['geometry']['coordinates']] + \
                      [[p[1], p[0]] for p in r2b_route['geometry']['coordinates']]
            
            # Crear camino Manhattan para visualización
            manhattan_path = [
                [end_airport['lat'], end_airport['lon']],
                [midpoint[0], midpoint[1]],
                [end_coords[0], end_coords[1]]
            ]
        else:
            # Ruta normal si está cerca
            r2 = requests.get(f"{OSRM_API_URL}/driving/{end_airport['lon']},{end_airport['lat']};{end_coords[1]},{end_coords[0]}?overview=full&geometries=geojson", timeout=15).json()
            if 'routes' not in r2 or not r2['routes']:
                raise Exception("No se pudo calcular la ruta terrestre desde el aeropuerto")
            r2_route = r2['routes'][0]
            ground2_dist_km = round(r2_route['distance'] / 1000, 2)
            ground2_duration_s = r2_route['duration']
            r2_geom = [[p[1], p[0]] for p in r2_route['geometry']['coordinates']]
            manhattan_path = None

        # Calcular detalles del vuelo
        flight_dist_km = round(haversine(start_airport['lat'], start_airport['lon'], end_airport['lat'], end_airport['lon']), 2)
        flight_duration_s = (flight_dist_km / 800) * 3600
        airport_time_s = 2.5 * 3600 # Tiempo extra en aeropuertos

        flight_details = {
            'duration_s': ground1_duration_s + flight_duration_s + ground2_duration_s + airport_time_s,
            'distance_km': flight_dist_km,
            'carbon_g': flight_dist_km * FLIGHT_CARBON_G_PER_KM,
            'route_type': 'multimodal_aerial',
            'ground1_duration_s': ground1_duration_s,
            'ground1_distance_km': ground1_dist_km,
            'ground2_duration_s': ground2_duration_s,
            'ground2_distance_km': ground2_dist_km,
            'total_distance_km': ground1_dist_km + flight_dist_km + ground2_dist_km
        }

        # Geometrías de las rutas para dibujar en el mapa
        flight_path = [[start_airport['lat'], start_airport['lon']], [end_airport['lat'], end_airport['lon']]]
        
        return jsonify({
            'status': 'success',
            'parts': {
                'ground1': r1_geom,
                'flight': flight_path,
                'ground2': r2_geom,
                'manhattan_path': manhattan_path
            },
            'airports': {
                'start': start_airport,
                'end': end_airport
            },
            'details': {
                'flight': flight_details
            }
        })
        
    except requests.RequestException as e:
        raise Exception(f"Error al calcular ruta multimodal: {str(e)}")

import os

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
