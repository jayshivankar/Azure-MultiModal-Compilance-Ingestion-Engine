FROM redis:7-alpine

# Set identical command as docker-compose for consistency
CMD ["redis-server", "--appendonly", "yes", "--save", "60", "1", "--loglevel", "warning"]
