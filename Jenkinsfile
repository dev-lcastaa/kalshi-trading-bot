pipeline {
    agent {
        label 'production'
    }

    options {
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    parameters {
        booleanParam(
            name: 'DEPLOY_COLLECTOR',
            defaultValue: false,
            description: 'Rebuild and restart the market-data collector. Leaves a ~80 s gap in every feed; only tick it when the collector code changed.'
        )
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Backend Lint') {
            steps {
                sh '''
                    python3 -m pip install --break-system-packages flake8 --quiet
                    flake8 src/kalshi_bot tests --count --select=E9,F63,F7,F82 --show-source --statistics || true
                '''
            }
        }

        stage('Backend Tests') {
            steps {
                sh '''
                    python3 -m pip install --break-system-packages -e . -e ".[dev,research]" --quiet
                    python3 -m pytest tests/ -v --tb=short
                '''
            }
        }

        stage('Frontend Lint') {
            steps {
                dir('frontend') {
                    sh '''
                        npm install --silent
                        npm run lint || true
                    '''
                }
            }
        }

        stage('Frontend Tests') {
            steps {
                dir('frontend') {
                    sh '''
                        npm run test:unit || true
                    '''
                }
            }
        }

        stage('E2E Tests') {
            steps {
                dir('frontend') {
                    sh '''
                        npm run test:e2e || true
                    '''
                }
            }
        }

        stage('Deploy') {
            steps {
                // The collector is deliberately left running: restarting it opens a ~80 s gap in every feed,
                // and the Phase 1 data gate counts gaps. Tick DEPLOY_COLLECTOR only when its own code changed.
                sh '''
                    docker compose up -d postgres
                    docker compose build --no-cache bot frontend
                    docker compose up -d --force-recreate --no-deps bot frontend
                    if [ "${DEPLOY_COLLECTOR}" = "true" ]; then
                        docker compose build collector
                        docker compose up -d --force-recreate --no-deps collector
                    else
                        docker compose up -d --no-deps collector
                    fi
                '''
            }
        }
    }

    post {
        failure {
            sh 'docker compose logs || true'
        }
    }
}
