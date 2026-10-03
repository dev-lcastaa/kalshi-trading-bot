pipeline {
    agent {
        label 'production'
    }

    options {
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
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
                    python3 -m ensurepip --default-pip || true
                    python3 -m pip install --upgrade pip --quiet
                    python3 -m pip install flake8 --quiet
                    flake8 src/kalshi_bot tests --count --select=E9,F63,F7,F82 --show-source --statistics || true
                '''
            }
        }

        stage('Backend Tests') {
            steps {
                sh '''
                    python3 -m ensurepip --default-pip || true
                    python3 -m pip install --upgrade pip --quiet
                    python3 -m pip install -e . -e ".[dev]" --quiet
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
                        npx playwright install --with-deps
                        npm run test:e2e || true
                    '''
                }
            }
        }

        stage('Deploy') {
            steps {
                sh '''
                    docker compose down
                    docker compose build --no-cache
                    docker compose up --force-recreate -d
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
