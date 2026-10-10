# Spring Boot producer (Java 21). Build context: the repository root.
#   docker build -f producer-service/Dockerfile -t settleagent/producer .
FROM eclipse-temurin:21-jdk AS build
WORKDIR /build/producer-service
COPY producer-service/.mvn .mvn
COPY producer-service/mvnw producer-service/pom.xml ./
RUN chmod +x mvnw && ./mvnw -B -q dependency:go-offline
COPY producer-service/src src
# Tests need ../data and ../contracts and run in CI (make test); the image build only packages.
RUN ./mvnw -B -q -DskipTests package && cp target/producer-service-*.jar /build/app.jar

FROM eclipse-temurin:21-jre
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=build /build/app.jar app.jar
COPY data/synthetic/dataset.json /app/data/dataset.json
ENV PRODUCER_FIXTURES=/app/data/dataset.json JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=75 -XX:+ExitOnOutOfMemoryError"
USER 10001
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
