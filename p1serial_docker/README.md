# built
Create a (Docker) image, see attached Dockerfile  
```
docker build -t p1serial/p1serial .
```

# run
Run on the same host (network = host) as influxDB    
```
docker run \
 --restart unless-stopped \
 --detach \
 --net=host \
 --name=p1serial \
 --device /dev/ttyUSB0:/dev/ttyUSB0 \
 -e PYTHONUNBUFFERED=0 \
 p1serial/p1serial
```

# check
JSON string is being logged in container  
```
docker logs p1serial
```
